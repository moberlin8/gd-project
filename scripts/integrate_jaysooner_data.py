#!/usr/bin/env python3
"""
Jaysooner Integration Script

Integrates data from the Jaysooner/gratefulgpt-scraper-dataset repo into
the GD RAG pipeline. Specifically:

1. Replicates the Archive.org metadata scraping from
   Gratefuldead_archive_org.py to capture additional fields (taper,
   transferer, runtime, subjects) that enhance search.

2. Replicates the deadcast_scraper_v2.py logic to pull Deadcast podcast
   transcripts from dead.net and add them as text embeddings.

This script does NOT use the Jaysooner code directly — it's a standalone
reimplementation that integrates with our existing data format.

Usage:
    python3 integrate_jaysooner_data.py  [--max-shows N] [--scrapers archive|deadcast|all]

Output:
    - data/jaysooner_ia_metadata.jsonl  (extended IA metadata)
    - data/jaysooner_deadcast_transcripts.jsonl  (podcast transcripts)
"""

import argparse
import json
import os
import re
import time
import logging
from pathlib import Path
from datetime import datetime

import requests
from bs4 import BeautifulSoup

# --- CONFIG ---
BASE_URL = "https://archive.org"
DEADNET_BASE = "https://www.dead.net"
DEADCAST_INDEX_URL = "https://www.dead.net/deadcast-index"
DATA_DIR = Path("/home/mao/DaveMatt/gd-project/data")
OUTPUT_DIR = DATA_DIR
LOG_DIR = Path("/home/mao/DaveMatt/gd-project/scripts")

DELAY = 3.25
USER_AGENT = "GD-RAT-Jaysooner-Integrator/1.0"

# Archive fields not currently captured by our scraper
EXTRA_METADATA_FIELDS = [
    'taper', 'transferer', 'runtime', 'source',
    'subject', 'lineage', 'files'
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "jaysooner_integration.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


def load_existing_setlists():
    """Load existing setlists to avoid re-scraping already processed shows."""
    combined_path = OUTPUT_DIR / "gd_comments_combined.json"
    if not combined_path.exists():
        return {}
    with open(combined_path) as f:
        data = json.load(f)
    return data.get('setlists', {})


def clean_html(text):
    """Clean HTML tags from text, similar to Jaysooner's clean_html."""
    if not text:
        return ''
    if isinstance(text, list):
        text = ' '.join(str(t) for t in text)
    # Replace <br> tags with newlines
    text = re.sub(r'<br\s*/?>', '\n', str(text))
    # Remove all other HTML tags
    text = re.sub(r'<[^>]+>', '', text)
    # Collapse whitespace
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n\s*\n+', '\n', text)
    return text.strip()


def fetch_ia_metadata(identifier):
    """Fetch additional metadata fields for an IA identifier."""
    url = f"{BASE_URL}/metadata/{identifier}"
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    if resp.status_code != 200:
        return None
    return resp.json()


def extract_extended_metadata(item_metadata):
    """Extract extended metadata fields not captured by our current scraper.

    Based on Jaysooner's extract_metadata() function.
    """
    metadata = {
        'identifier': '',
        'title': '',
        'date': '',
        'venue': '',
        'description': '',
        'setlist': '',
        'subject': [],
        # Extra fields from Jaysooner
        'taper': '',
        'transferer': '',
        'runtime': '',
        'source': '',
        'creator': '',
    }

    metadata['identifier'] = item_metadata.get('identifier', '')

    # Title
    metadata['title'] = item_metadata.get('title', '')

    # Date — try multiple fields (same as Jaysooner)
    date_fields = ['date', 'performance_date', 'year', 'coverage']
    for field in date_fields:
        if field in item_metadata and item_metadata[field]:
            metadata['date'] = str(item_metadata[field])
            break

    # Venue — try multiple fields
    venue_fields = ['venue', 'location', 'coverage', 'spatial']
    for field in venue_fields:
        if field in item_metadata and item_metadata[field]:
            metadata['venue'] = str(item_metadata[field])
            break

    # Description
    description = item_metadata.get('description', '')
    if description:
        metadata['description'] = clean_html(description)

    # Setlist — look in description/notes/lineage fields
    setlist_fields = ['setlist', 'notes', 'lineage', 'track']
    setlist_text = ""
    for field in setlist_fields:
        if field in item_metadata and item_metadata[field]:
            field_value = item_metadata[field]
            if isinstance(field_value, list):
                field_value = ' '.join(str(v) for v in field_value)
            setlist_text += f" {field_value}"
    metadata['setlist'] = clean_html(setlist_text.strip())

    # Subject/tags
    subject = item_metadata.get('subject', [])
    if isinstance(subject, str):
        subject = [subject]
    elif not isinstance(subject, list):
        subject = []
    metadata['subject'] = [str(s) for s in subject]

    # Extra fields (our new additions)
    metadata['creator'] = item_metadata.get('creator', '')
    metadata['source'] = item_metadata.get('source', '')
    metadata['taper'] = item_metadata.get('taper', '')
    metadata['transferer'] = item_metadata.get('transferer', '')
    metadata['runtime'] = item_metadata.get('runtime', '')

    return metadata


def scrape_extended_ia_metadata(max_shows=None):
    """Scrape extended IA metadata for shows that are already in our setlists.

    Uses archive.org advanced search API to find GD shows, then fetches
    detailed metadata for each to extract taper/transferer/runtime/subjects.
    """
    existing = load_existing_setlists()
    existing_ids = set(existing.keys())
    logger.info(f"Found {len(existing_ids)} existing setlist IDs to enrich")

    # Search IA for the same shows
    search_url = f"{BASE_URL}/advancedsearch.php"
    params = {
        'q': f'collection:GratefulDead AND creator:"Grateful Dead"',
        'fl': 'identifier,title,date,venue,description',
        'rows': 1000,
        'start': 0,
        'output': 'json'
    }
    headers = {"User-Agent": USER_AGENT}

    enriched = []
    processed = 0
    page_start = 0

    while True:
        params['start'] = page_start
        resp = requests.get(search_url, params=params, headers=headers, timeout=60)
        if resp.status_code != 200:
            logger.error(f"Search failed at start={page_start}: {resp.status_code}")
            break

        data = resp.json()
        docs = data.get('response', {}).get('docs', [])
        if not docs:
            break

        logger.info(f"Search page: {len(docs)} docs (start={page_start})")

        for doc in docs:
            identifier = doc.get('identifier')
            if identifier in existing_ids:
                # Enrich existing show
                meta = fetch_ia_metadata(identifier)
                if meta:
                    item_metadata = meta.get('metadata', {})
                    extended = extract_extended_metadata(item_metadata)

                    # Also extract file info (track list)
                    files = meta.get('files', [])
                    audio_files = []
                    for f in files:
                        fmt = f.get('format', '')
                        if fmt in ['VBR MP3', 'FLAC', 'Ogg Vorbis', 'Shorten', 'WAV', 'MP3']:
                            audio_files.append({
                                'name': f.get('name', ''),
                                'title': f.get('title', ''),
                                'length': f.get('length', ''),
                                'format': fmt
                            })

                    extended['audio_files'] = audio_files

                    enriched.append(extended)
                    processed += 1

                    if processed % 10 == 0:
                        logger.info(f"  Enriched {processed} shows...")

                    if max_shows and processed >= max_shows:
                        break

                    time.sleep(DELAY)
                # Don't sleep after every show — only ones we actually process

            if max_shows and processed >= max_shows:
                break

        page_start += len(docs)

        if not docs or (len(docs) < 1000):
            break

        if max_shows and processed >= max_shows:
            break

    # Save to JSONL
    output_path = OUTPUT_DIR / "jaysooner_ia_metadata.jsonl"
    with open(output_path, 'w') as f:
        for item in enriched:
            f.write(json.dumps(item, ensure_ascii=False) + '\n')

    logger.info(f"Saved {len(enriched)} enriched metadata entries to {output_path}")
    return enriched


def scrape_deadcast_transcripts(max_episodes=None):
    """Scrape Deadcast podcast transcripts from dead.net.

    Based on Jaysooner's deadcast_scraper_v2.py.
    """
    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64; rv:121.0) Gecko/20100101 Firefox/121.0',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
        'Accept-Encoding': 'gzip, deflate',
        'Referer': 'https://www.google.com/',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
    })

    logger.info("Fetching Deadcast episode links from index page...")
    resp = session.get(DEADCAST_INDEX_URL, timeout=30)
    if resp.status_code != 200:
        logger.error(f"Failed to fetch index: {resp.status_code}")
        return []

    soup = BeautifulSoup(resp.text, 'html.parser')

    # Find all deadcast episode links
    episode_links = []
    seen_urls = set()

    for a in soup.find_all('a', href=True):
        href = a['href']
        if '/episodes/' in href or '/deadcast/' in href or 'episode' in href.lower():
            full_url = href if href.startswith('http') else f"{DEADNET_BASE}{href}"
            if full_url not in seen_urls:
                title = a.get_text(strip=True) or a.get('title', '')
                if title:
                    seen_urls.add(full_url)
                    episode_links.append({
                        'url': full_url,
                        'title': title
                    })

    logger.info(f"Found {len(episode_links)} episode links")

    if max_episodes:
        episode_links = episode_links[:max_episodes]

    transcripts = []
    progress_file = OUTPUT_DIR / "deadcast_progress.json"
    scraped_urls = set()
    if progress_file.exists():
        with open(progress_file) as f:
            progress = json.load(f)
            scraped_urls = set(progress.get('scraped_urls', []))

    for i, ep in enumerate(episode_links, 1):
        if ep['url'] in scraped_urls:
            logger.info(f"  [{i}/{len(episode_links)}] Skipping (already scraped): {ep['title'][:60]}")
            continue

        logger.info(f"  [{i}/{len(episode_links)}] Scraping: {ep['title'][:60]}...")

        resp = session.get(ep['url'], timeout=30)
        if resp.status_code != 200:
            logger.warning(f"  Failed: HTTP {resp.status_code}")
            continue

        soup = BeautifulSoup(resp.text, 'html.parser')

        # Look for transcript content
        transcript_text = ""
        selectors = [
            '.field-name-field-transcript',
            '.transcript',
            '.episode-transcript',
            '#transcript-content',
            '.node__content .transcript',
            '.content .transcript'
        ]
        for sel in selectors:
            el = soup.select_one(sel)
            if el:
                transcript_text = el.get_text(strip=False)
                break

        if not transcript_text:
            # Try finding any large text block
            paragraphs = soup.find_all('p')
            text_parts = []
            for p in paragraphs:
                text = p.get_text(strip=True)
                if len(text) > 50:
                    text_parts.append(text)
            if text_parts:
                transcript_text = '\n\n'.join(text_parts)

        transcript_text = transcript_text.strip() if transcript_text else ""

        if transcript_text:
            transcripts.append({
                'title': ep['title'],
                'url': ep['url'],
                'transcript': transcript_text,
                'word_count': len(transcript_text.split()),
                'timestamp': datetime.now().isoformat()
            })
            scraped_urls.add(ep['url'])
            logger.info(f"  ✓ Saved ({len(transcript_text.split())} words)")
        else:
            logger.info(f"  ✗ No transcript found")

        # Save progress
        with open(progress_file, 'w') as f:
            json.dump({
                'scraped_urls': list(scraped_urls),
                'total_episodes': len(episode_links),
                'timestamp': datetime.now().isoformat()
            }, f)

        time.sleep(DELAY)

    # Save transcripts
    output_path = OUTPUT_DIR / "jaysooner_deadcast_transcripts.jsonl"
    with open(output_path, 'a') as f:
        for t in transcripts:
            f.write(json.dumps(t, ensure_ascii=False) + '\n')

    logger.info(f"Saved {len(transcripts)} deadcast transcripts to {output_path}")
    return transcripts


def integrate_into_combined():
    """Merge Jaysooner data into the combined GD dataset."""
    combined_path = OUTPUT_DIR / "gd_comments_combined.json"
    if not combined_path.exists():
        logger.warning("Combined data file not found — skipping integration")
        return

    with open(combined_path) as f:
        data = json.load(f)

    # Integrate extended IA metadata
    ia_path = OUTPUT_DIR / "jaysooner_ia_metadata.jsonl"
    if ia_path.exists():
        with open(ia_path) as f:
            for line in f:
                if not line.strip():
                    continue
                entry = json.loads(line)
                show_id = entry.get('identifier', '')
                if show_id in data.get('setlists', {}):
                    # Add extended fields to existing setlist
                    sl = data['setlists'][show_id]
                    if isinstance(sl, dict):
                        sl.setdefault('extended_metadata', {})
                        for field in ['taper', 'transferer', 'runtime', 'source', 'subjects']:
                            val = entry.get(field.replace('subjects', 'subject'), '')
                            if val:
                                sl['extended_metadata'][field] = val
                        # Add audio file titles
                        if entry.get('audio_files'):
                            sl.setdefault('audio_file_titles', {})
                            for f in entry['audio_files']:
                                sl['audio_file_titles'][f['name']] = f.get('title', '')

        logger.info(f"Integrated extended IA metadata for {len(data['setlists'])} setlists")

    # Integrate deadcast transcripts
    dc_path = OUTPUT_DIR / "jaysooner_deadcast_transcripts.jsonl"
    if dc_path.exists():
        data.setdefault('deadcast_transcripts', [])
        with open(dc_path) as f:
            for line in f:
                if not line.strip():
                    continue
                data['deadcast_transcripts'].append(json.loads(line))

        logger.info(f"Added {len(data['deadcast_transcripts'])} deadcast transcripts")

    with open(combined_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    logger.info(f"Updated combined data: {combined_path}")


def main():
    parser = argparse.ArgumentParser(description="Integrate Jaysooner/scraper-dataset data")
    parser.add_argument("--max-shows", type=int, default=None,
                        help="Max IA shows to enrich")
    parser.add_argument("--max-episodes", type=int, default=None,
                        help="Max Deadcast episodes to scrape")
    parser.add_argument("--scrapers", choices=['archive', 'deadcast', 'all'],
                        default='all', help="Which data sources to scrape")
    parser.add_argument("--integrate", action="store_true",
                        help="Integrate scraped data into combined dataset")

    args = parser.parse_args()

    if args.scrapers in ('archive', 'all') and not args.integrate:
        logger.info("=== Scraping Extended IA Metadata ===")
        scrape_extended_ia_metadata(max_shows=args.max_shows)

    if args.scrapers in ('deadcast', 'all') and not args.integrate:
        logger.info("\n=== Scraping Deadcast Transcripts ===")
        scrape_deadcast_transcripts(max_episodes=args.max_episodes)

    if args.integrate:
        logger.info("\n=== Integrating into Combined Dataset ===")
        integrate_into_combined()

    logger.info("\nDone!")


if __name__ == "__main__":
    main()
