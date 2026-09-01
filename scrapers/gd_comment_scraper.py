#!/usr/bin/env python3
"""
GD Comment Scraper — Incremental Overnight Mode

Designed to run periodically (e.g., nightly 2-5am) to systematically
scrape the entire GratefulDead collection on archive.org.

Features:
- Resumeable: tracks processed show IDs in a state file
- Year-based pagination: iterates 1965-1995 to avoid IA's non-random "random" sort
- Configurable: target shows per run, delay, start year
- Incremental saves: progress saved every 10 shows
- Setlist capture: parses song lists from IA metadata descriptions

Usage:
    # Default: scrape up to 50 shows, continue from last position
    python3 gd_comment_scraper.py

    # Custom target (e.g., for overnight window)
    python3 gd_comment_scraper.py --target 200

    # Force restart from a specific year
    python3 gd_comment_scraper.py --start-year 1975

    # Dry run (show what would be scraped)
    python3 gd_comment_scraper.py --dry-run

Environment:
    GD_SCRAPER_STATE_FILE  (default: data/scraper_state.json)
"""

import argparse
import json
import os
import time
import signal
import logging
from datetime import datetime
from pathlib import Path
import requests

# --- CONFIG ---
BASE_URL = "https://archive.org"
COLLECTION = "GratefulDead"
SAMPLE_SIZE = 8
ROWS_PER_PAGE = 50  # IA search rows max is 100, but 50 is safe for response size
DAYS_PER_YEAR = 365
# Calculate approximate number of shows per year needed to exhaust a year
MAX_ROWS = 100  # IA hard limit for 'rows' parameter
DELAY_SECONDS = 3.25
OUTPUT_DIR = Path("/home/mao/DaveMatt/gd-project/data")
LOG_DIR = Path("/home/mao/DaveMatt/gd-project/scripts")
STATE_FILE = Path(os.environ.get(
    "GD_SCRAPER_STATE_FILE",
    OUTPUT_DIR / "scraper_state.json"
))
COMBINED_OUTPUT = OUTPUT_DIR / "gd_comments_combined.json"

# Keywords to filter relevant comments (songs, experiences, music reviews)
RELEVANT_KEYWORDS = [
    "song", "jam", "play", "show", "set", "music", "sound",
    "experience", "played", "grateful", "dead", "band",
    "performance", "version", "transition", "segue",
    "audience", "crowd", "venue", "atmosphere",
    "dark star", "turn on", "estimated", "eyes", "truckin",
    "bertha", "terrapin", "china cat", "morning dew",
    "good lovin", "uncle john", "sugar magnolia",
]

# --- SETUP ---
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "gd_scraper.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


def load_state() -> dict:
    """Load scraper state (processed IDs, current year, stats)."""
    if STATE_FILE.exists():
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {
        "processed_ids": [],
        "current_year": 1965,
        "shows_with_comments": 0,
        "total_comments": 0,
        "total_setlists": 0,
        "last_save": "never",
    }


def save_state(state: dict):
    """Save scraper state."""
    state["last_save"] = datetime.now().isoformat()
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def load_existing_data() -> dict:
    """Load combined data file if it exists (for merging)."""
    if COMBINED_OUTPUT.exists():
        with open(COMBINED_OUTPUT, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "experiment": "GD Comment RAT — Full Collection Crawl",
        "collection": COLLECTION,
        "metadata": {},
        "shows_processed": [],
        "setlists": {},
        "comments": [],
    }


def save_combined_data(state: dict, all_comments: list, all_setlists: dict):
    """Save merged dataset to the canonical combined file."""
    data = load_existing_data()
    data["comments"] = all_comments
    data["setlists"] = all_setlists
    data["shows_processed"] = state["processed_ids"]
    data["metadata"] = {
        "shows_attempted": len(state["processed_ids"]),
        "shows_with_comments": state["shows_with_comments"],
        "comments_total": state["total_comments"],
        "setlists_total": state["total_setlists"],
        "last_updated": datetime.now().isoformat(),
        "last_save": state["last_save"],
    }
    data["timestamp"] = datetime.now().isoformat()
    with open(COMBINED_OUTPUT, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def search_shows_paginated(year: int, processed_ids: set, target_count: int) -> list[str]:
    """
    Fetch ALL Grateful Dead identifiers from IA for a year, paginating through results.
    Stops early if we've found enough new (unprocessed) shows.

    Uses IA's 'start' parameter for pagination, max 100 rows per page.
    """
    url = f"{BASE_URL}/advancedsearch.php"
    query = f"collection:{COLLECTION} AND creator:Grateful Dead AND date:[{year}-01-01 TO {year}-12-31]"
    headers = {"User-Agent": "GD-RAT-Scraper/2.0-Incremental"}
    all_identifiers = []
    start = 0
    rows_per_page = 100
    max_pages = 50  # safety limit

    for page_num in range(max_pages):
        params = {
            "q": query,
            "fl": "identifier",
            "rows": rows_per_page,
            "start": start,
            "output": "json"
        }
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            docs = data.get("response", {}).get("docs", [])
            if not docs:
                break

            new_found = 0
            for doc in docs:
                identifier = doc["identifier"]
                if identifier not in processed_ids:
                    all_identifiers.append(identifier)
                    processed_ids.add(identifier)
                    new_found += 1
                    # Stop early if we have enough
                    if len(all_identifiers) >= target_count:
                        return all_identifiers

            start += rows_per_page
            total_responses = start + len(docs)
            logger.info(f"  Year {year}: fetched {total_responses} total, {new_found} new this page")

            # If this page was full, there might be more — continue
            # If it wasn't full, we've reached the end
            if len(docs) < rows_per_page:
                break

            time.sleep(0.1)  # short pause between pages for same-year requests

        except Exception as e:
            logger.error(f"  Search failed for {year} page {page_num}: {e}")
            break

    logger.info(f"  Year {year}: collected {len(all_identifiers)} new shows (total fetched so far: {len(processed_ids)})")
    return all_identifiers


def fetch_comments(identifier: str) -> tuple[list[dict], dict]:
    """
    Fetch user reviews/comments AND setlist metadata for a single IA item.
    Uses the metadata endpoint: https://archive.org/metadata/{identifier}
    Returns: (reviews, setlist_dict)
    """
    url = f"{BASE_URL}/metadata/{identifier}"
    headers = {"User-Agent": "GD-RAT-Scraper/2.0-Incremental"}
    logger.info(f"  Fetching: {identifier}")

    max_retries = 3
    base_delay = 5
    for attempt in range(max_retries):
        try:
            resp = requests.get(url, headers=headers, timeout=30)
            if resp.status_code == 404:
                logger.warning(f"    404: {identifier}")
                return [], {}
            resp.raise_for_status()
            break
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            if attempt < max_retries - 1:
                wait = base_delay * (2 ** attempt)
                logger.warning(f"    Request timeout (attempt {attempt + 1}/{max_retries}), retrying in {wait}s...")
                time.sleep(wait)
            else:
                logger.error(f"    Failed after {max_retries} attempts: {e}")
                return [], {}
        except requests.exceptions.RequestException as e:
            logger.error(f"    HTTP error for {identifier}: {e}")
            return [], {}
    else:
        return [], {}

    try:
        data = resp.json()
    except (json.JSONDecodeError, ValueError) as e:
        logger.error(f"    JSON decode error for {identifier}: {e}")
        return [], {}

        reviews = data.get("reviews", [])

        # Extract setlist from description
        metadata = data.get("metadata", {})
        desc = metadata.get("description", "")
        
        # Initialize empty setlist structure
        setlist = {
            'sets': [],
            'songs': [],
            'transitions': [],
            'guest_artists': [],
            'song_durations': {},
            'raw_description': desc.strip(),
            'venue': metadata.get('venue', ''),
            'date': metadata.get('date', ''),
            'creator': metadata.get('creator', '')
        }
        
        if desc:
            parsed = parse_setlist(desc)
            setlist.update(parsed)
        
        # Extract track durations from files array
        if 'files' in data:
            for file_info in data['files']:
                if isinstance(file_info, dict):
                    name = file_info.get('name', '')
                    length = file_info.get('length', '')
                    fmt = file_info.get('format', '')
                    title = file_info.get('title', '')
                    
                    # Map file names to song names
                    # File naming pattern: gdYY-MM-DDd[d]t[track_num].xxx
                    if name and length and fmt in ['VBR MP3', 'FLAC', 'Ogg Vorbis']:
                        track_key = name.replace('.flac', '').replace('.mp3', '').replace('.ogg', '')
                        setlist['song_durations'][track_key] = {
                            'length': length,
                            'format': fmt,
                            'title': title
                        }
        
        # Map song positions to track durations (after both setlist and files are parsed)
        # The order of songs in the setlist corresponds to the order of audio tracks
        setlist = map_durations_to_songs(setlist)

        if reviews:
            logger.info(f"    Found {len(reviews)} comments")
            if setlist.get('songs'):
                logger.info(f"    Setlist: {len(setlist['songs'])} songs across {len(setlist.get('sets', []))} sets")
                if setlist.get('guest_artists'):
                    logger.info(f"    Guest artists: {', '.join(setlist['guest_artists'])}")
                if setlist.get('transitions'):
                    logger.info(f"    Transitions: {len(setlist['transitions'])} segues detected")
        else:
            logger.info(f"    No comments for this item")

        return reviews, setlist

    except json.JSONDecodeError:
        logger.warning(f"    Invalid JSON for {identifier}")
        return [], {}
    except Exception as e:
        logger.error(f"    Error: {e}")
        return [], {}


def parse_setlist(description: str) -> dict:
    """
    Parse setlist from IA description field.
    Returns structured data with sets, songs, transitions, and guest artists.
    """
    import re
    
    # Strip HTML tags but preserve <br /> and structural markers
    desc = re.sub(r'<br\s*/?>', '\n', description)
    desc = re.sub(r'<[^>]+>', ' ', desc)
    # Normalize whitespace but preserve newlines for set detection
    lines = [line.strip() for line in desc.split('\n') if line.strip()]
    
    setlist = {
        'sets': [],
        'songs': [],
        'transitions': [],
        'guest_artists': [],
        'raw_description': description.strip()
    }
    
    if not lines:
        return setlist
    
    # Extract guest artists
    guest_match = re.search(r'Other artist\(s\):\s*(.+?)(?:\n|$)', desc, re.IGNORECASE)
    if guest_match:
        guests = guest_match.group(1).strip()
        setlist['guest_artists'] = [g.strip() for g in guests.split(',') if g.strip()]
    
    current_set = None
    set_order = 0
    
    for line in lines:
        # Detect set boundaries
        set_match = re.match(r'Set\s*([0-9])|^Set\s*One|^Set\s*Two|^Set\s*Three|Disc\s*\d|Segue\s*\d', line, re.IGNORECASE)
        encore_match = re.match(r'Encore\s*[0-9]?', line, re.IGNORECASE)
        
        if set_match:
            # Determine set name/number
            num_match = re.search(r'[0-9]', set_match.group(0))
            set_name = f"Set {num_match.group(0)}" if num_match else set_match.group(0)
            current_set = {
                'name': set_name,
                'order': set_order,
                'songs': []
            }
            setlist['sets'].append(current_set)
            set_order += 1
            continue
            
        if encore_match:
            current_set = {
                'name': 'Encore',
                'order': set_order,
                'songs': []
            }
            setlist['sets'].append(current_set)
            set_order += 1
            continue
        
        # Skip non-song metadata lines
        if re.match(r'(Other artist|Notes:|Source|Soundline|MMA|Recorded|Transfer)', line, re.IGNORECASE):
            continue
        
        # Parse songs in current line
        if current_set:
            # Handle both newline-separated and comma-separated formats
            song_items = line.split(',') if ',' in line else [line]
            
            for item in song_items:
                item = item.strip()
                if not item or re.match(r'(Set|Disc|Encore)', item, re.IGNORECASE):
                    continue
                
                song_entry = {'name': '', 'transition': False, 'guest_on_song': []}
                
                # Check for transition marker
                if '->' in item:
                    parts = item.split('->')
                    song_entry['name'] = parts[0].strip()
                    song_entry['transition'] = True
                    setlist['transitions'].append({
                        'from': parts[0].strip(),
                        'to': parts[1].strip() if len(parts) > 1 else ''
                    })
                elif '>' in item:
                    parts = item.split('>')
                    song_entry['name'] = parts[0].strip()
                    song_entry['transition'] = True
                    setlist['transitions'].append({
                        'from': parts[0].strip(),
                        'to': parts[1].strip() if len(parts) > 1 else ''
                    })
                else:
                    song_entry['name'] = re.sub(r'\s+', ' ', item)
                
                # Extract special notation markers (*, ^, etc.) 
                notation_match = re.findall(r'[*^\d]+(?=\s|$)', song_entry['name'])
                if notation_match:
                    song_entry['notations'] = notation_match
                    song_entry['name'] = re.sub(r'[*^\d\s]+$', '', song_entry['name']).strip()
                
                if song_entry['name']:
                    current_set['songs'].append(song_entry)
                    setlist['songs'].append(song_entry['name'])
    
    return setlist




def map_durations_to_songs(setlist: dict) -> dict:
    """Map track file durations to song positions in the setlist.

    Primary method: Use IA file metadata 'title' field for direct
    song-name-to-track matching. This is far more accurate than positional
    matching because it doesn't depend on setlist and file ordering aligning.

    Fallback: Positional matching when files lack 'title' fields.
    """
    import re

    song_durations = setlist.get('song_durations', {})
    songs = setlist.get('songs', [])
    if not song_durations or not songs:
        return setlist

    # Build ordered song names from the sets structure
    ordered_song_names = []
    for s in setlist.get('sets', []):
        for se in s['songs']:
            name = se.get('name', '').strip()
            if name:
                ordered_song_names.append(name)
    if not ordered_song_names:
        ordered_song_names = list(songs)

    # Sort track files by track number extraction
    def extract_track_num(key):
        """Extract track number from key like 'gd77-05-07d1t02' -> 2"""
        m = re.search(r't(\d+)', key)
        if m:
            return int(m.group(1))
        m = re.search(r'^(\d+)', key)
        if m:
            return int(m.group(1))
        return 9999

    track_files = sorted(song_durations.keys(), key=extract_track_num)

    # Strategy 1: Direct title matching — use IA file 'title' field
    # for direct song-name-to-track matching, eliminating positional issues.
    non_song_phrases = {
        'tuning', 'comment', 'crowd noise', 'applause', 'banter',
        'announcement', 'speech', 'fade in', 'fade out'
    }

    song_track_map = {}
    used_tracks = set()

    for track_key in track_files:
        track_info = song_durations[track_key]
        title = track_info.get('title', '').strip()
        if not title:
            continue

        # Clean title — remove transition markers
        title_clean = title.replace('->', '').replace('&gt;', '').strip()
        title_lower = title_clean.lower()
        if any(nsp in title_lower for nsp in non_song_phrases):
            continue

        # Try exact match against setlist songs
        for song_name in ordered_song_names:
            if song_name.lower() == title_clean.lower():
                song_track_map[song_name] = {
                    'length': track_info['length'],
                    'format': track_info.get('format', ''),
                    'track_file': track_key,
                    'title': title
                }
                used_tracks.add(track_key)
                break

    # Strategy 2: Positional fallback for remaining unmatched songs
    remaining_tracks = [k for k in track_files if k not in used_tracks]
    remaining_songs = [s for s in ordered_song_names if s not in song_track_map]

    if remaining_tracks and remaining_songs:
        for i, song_name in enumerate(remaining_songs):
            if i < len(remaining_tracks):
                track_key = remaining_tracks[i]
                track_info = song_durations[track_key]
                song_track_map[song_name] = {
                    'length': track_info['length'],
                    'format': track_info.get('format', ''),
                    'track_file': track_key,
                    'title': track_info.get('title', '')
                }

    # Attach durations to song entries in sets
    for s in setlist.get('sets', []):
        for se in s['songs']:
            song_name = se.get('name', '').strip()
            if song_name in song_track_map:
                se['duration'] = song_track_map[song_name]['length']
                se['track_file'] = song_track_map[song_name]['track_file']

    # Also add durations to the song_durations dict keyed by song name
    for song_name, info in song_track_map.items():
        song_durations[song_name] = info

    setlist['song_durations'] = song_durations
    return setlist

def filter_relevant_comment(text: str) -> bool:
    """Check if a comment mentions songs, experiences, or music reviews."""
    if not text or len(text.strip()) < 10:
        return False
    lowered = text.lower()
    return any(kw in lowered for kw in RELEVANT_KEYWORDS)


def extract_comment_data(identifier: str, review: dict) -> dict:
    """Normalize a review dict into our structured format."""
    comment_text = review.get("reviewbody", "")
    comment_text = " ".join(comment_text.split()) if comment_text else ""

    return {
        "show_identifier": identifier,
        "comment_id": review.get("reviewidentifier", ""),
        "reviewer": review.get("username", "") or "anonymous",
        "comment_text": comment_text,
        "rating": review.get("stars", None),
        "created": review.get("reviewdate", ""),
        "is_relevant": filter_relevant_comment(comment_text),
    }


def main():
    parser = argparse.ArgumentParser(description="GD Comment Scraper — Incremental Overnight Mode")
    parser.add_argument("--target", type=int, default=50,
                        help="Number of shows to scrape this run (default: 50)")
    parser.add_argument("--start-year", type=int, default=None,
                        help="Force start year (overrides saved state)")
    parser.add_argument("--delay", type=float, default=2.0,
                        help="Delay between requests in seconds (default: 2.0)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be scraped without making requests")
    args = parser.parse_args()

    global DELAY_SECONDS
    DELAY_SECONDS = args.delay

    logger.info("=" * 60)
    logger.info("GD Comment Scraper — Incremental Overnight Mode")
    logger.info(f"Target this run: {args.target} shows | Max delay: {DELAY_SECONDS}s")
    logger.info(f"Combined data: {COMBINED_OUTPUT}")
    logger.info(f"State file: {STATE_FILE}")
    logger.info("=" * 60)

    # Load state and existing data
    state = load_state()

    # Hard guard: ensure year range is always 1965-1995 (inclusive)
    # This prevents corrupted state files from extending beyond 1995
    if state["current_year"] < 1965:
        state["current_year"] = 1965
    if state["current_year"] > 1995:
        state["current_year"] = 1995

    existing_data = load_existing_data()
    all_comments = existing_data.get("comments", [])
    all_setlists = existing_data.get("setlists", {})

    if args.start_year is not None:
        state["current_year"] = args.start_year

    processed_count = 0
    start_time = time.time()

    # Convert processed_ids to a set for fast lookup, but keep the list for state saving
    processed_ids_set = set(state["processed_ids"])

    # Graceful shutdown handler — saves state on SIGTERM/SIGINT
    shutdown_requested = False

    def handle_signal(signum, frame):
        nonlocal shutdown_requested
        logger.info(f"\n{'!' * 60}")
        logger.info(f"SHUTDOWN SIGNAL received (signal {signum}) — saving state and exiting gracefully...")
        logger.info(f"{'!' * 60}")
        shutdown_requested = True

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    # Iterate through years until target reached
    while processed_count < args.target and state["current_year"] <= 1995:
        year = state["current_year"]
        identifiers = search_shows_paginated(year, processed_ids_set, args.target - processed_count)
        state["current_year"] += 1

        if not identifiers:
            logger.info(f"No shows for {year}, moving to {year + 1}")
            continue

        if args.dry_run:
            logger.info(f"[DRY RUN] Would process {len(identifiers)} shows from {year}:")
            for ident in identifiers:
                logger.info(f"  - {ident}")
            break

        for identifier in identifiers:
            if processed_count >= args.target:
                break
            if shutdown_requested:
                logger.info("Shutdown requested — exiting loop")
                break
            if identifier in state["processed_ids"]:
                logger.info(f"  Skipping {identifier} (already processed)")
                continue

            state["processed_ids"].append(identifier)
            processed_ids_set.add(identifier)
            processed_count += 1
            logger.info(f"\n[Run attempt {processed_count}/{args.target}] {identifier}")

            raw_reviews, setlist = fetch_comments(identifier)
            if raw_reviews:
                state["shows_with_comments"] += 1
                for review in raw_reviews:
                    comment_data = extract_comment_data(identifier, review)
                    if comment_data["is_relevant"]:
                        all_comments.append(comment_data)
                        logger.info(f"    ✓ Relevant: {comment_data['reviewer']}")
                state["total_comments"] += len([c for c in all_comments if c["show_identifier"] == identifier])
            else:
                logger.info(f"    No comments for this item")

            if setlist:
                all_setlists[identifier] = setlist
                state["total_setlists"] += 1
                song_count = len(setlist.get('songs', []))
                logger.info(f"    Setlist: {song_count} songs" if song_count else "")

            # Rate limiting
            time.sleep(DELAY_SECONDS)

        # Check for shutdown after each year's batch
        if shutdown_requested:
            break

        # Incremental save every 10 shows or at end of year
        if processed_count % 10 == 0 or processed_count == args.target:
            save_state(state)
            save_combined_data(state, all_comments, all_setlists)
            logger.info(f"  Progress saved: {len(state['processed_ids'])} total shows, "
                       f"{state['shows_with_comments']} with comments, "
                       f"{len(all_comments)} total comments")

        # Save state at end of each year
        save_state(state)
        logger.info(f"Year {year} done. Total: {len(state['processed_ids'])} | "
                   f"With comments: {state['shows_with_comments']}")

    # Final save (always, even if shutdown was requested)
    save_state(state)
    save_combined_data(state, all_comments, all_setlists)
    if shutdown_requested:
        logger.info(f"\n{'!' * 60}")
        logger.info(f"Graceful shutdown complete. State saved at {len(state['processed_ids'])} shows.")
        logger.info(f"Next run will resume from year {state['current_year']}.")
        logger.info(f"{'!' * 60}")

    elapsed_min = (time.time() - start_time) / 60
    logger.info(f"\n{'=' * 60}")
    logger.info(f"DONE — This run: {processed_count} shows processed")
    logger.info(f"Total shows: {len(state['processed_ids'])} | "
               f"With comments: {state['shows_with_comments']} | "
               f"Comments: {len(all_comments)}")
    logger.info(f"Time: {elapsed_min:.1f} minutes")
    logger.info(f"Next year to scrape: {state['current_year']}")
    logger.info(f"Combined data: {COMBINED_OUTPUT} ({COMBINED_OUTPUT.stat().st_size / 1024:.0f} KB)")
    logger.info(f"{'=' * 60}")


if __name__ == "__main__":
    main()
