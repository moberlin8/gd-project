#!/usr/bin/env python3
"""
Estimated Prophet Duration Extractor (v4)

Extracts all Estimated Prophet performances from the GD collection
on archive.org and ranks them by duration.

Key improvement over v3: Uses IA file metadata 'title' field for direct
song-name-to-track matching instead of positional matching. This is far
more accurate because:

1. Each IA audio file has a 'title' field containing the song name
   (e.g., "Estimated Prophet", "Tuning", "Drums ->")
2. We can directly search for files whose title matches our target song
3. This eliminates all alignment issues between setlist descriptions,
   DeadTracks track listings, and file ordering

DeadTracks.com is used as a secondary source when IA file titles
don't contain the song name (some older uploads don't have titles).
"""

import json
import re
import time
import requests
from pathlib import Path
from html import unescape

BASE_URL = "https://archive.org"
DELAY = 3.25
DEADTRACKS_BASE = "https://deadtracks.com"

# Estimated Prophet song_id on DeadTracks
EP_SONG_ID = 10

# Non-song track names to skip during matching
NON_SONG_TRACKS = {
    'tuning', 'announcement', 'crowd', 'intro', 'outro', 'banter',
    'speech', 'comment', 'applause', 'noise', 'misc', 'idling',
    'equipment problems', 'fade in', 'fade out', 'silence'
}


def duration_to_seconds(dur_str):
    """Convert duration string like '7:36' or '456.5' to seconds."""
    if not dur_str:
        return None

    # Try MM:SS format
    match = re.match(r'(\d+):(\d{1,2})', str(dur_str))
    if match:
        minutes = int(match.group(1))
        seconds = int(match.group(2))
        return minutes * 60 + seconds

    # Try seconds format (float-like string)
    try:
        return float(dur_str)
    except (ValueError, TypeError):
        return None


def parse_setlist_for_songs(desc):
    """Parse IA description to get a flat list of song names in order."""
    desc = re.sub(r'<br\s*/?>', '\n', desc)
    desc = re.sub(r'<[^>]+>', ' ', desc)
    lines = [line.strip() for line in desc.split('\n') if line.strip()]

    # Non-song phrases that should never be counted as songs
    skip_phrases = {
        'comment', 'crew', 'sound', 'light', 'stage', 'monitors',
        'other artist', 'guests', 'guest', 'band', 'members',
        'jerry garcia', 'bob weir', 'phil lesh', 'bill kreutzmann',
        'mickey hart', 'keith godchaux', 'donna jean', 'ronn cuomo',
        'donna godchaux', 'vocals', 'guitar', 'bass', 'drums', 'keyboards',
        'misc', 'tune', 'banter', 'announcement', 'speech'
    }

    songs = []
    for line in lines:
        line_lower = line.lower()
        if re.match(r'(Set|Disc|Encore)', line, re.IGNORECASE):
            continue
        if 'other artist' in line_lower or 'guests' in line_lower:
            continue
        if any(skip in line_lower for skip in skip_phrases):
            continue

        song_items = line.split(',') if ',' in line else [line]
        for item in song_items:
            item = item.strip()
            if item and not re.match(r'(Set|Disc|Encore)', item, re.IGNORECASE):
                item_lower = item.lower()
                if any(skip in item_lower for skip in skip_phrases):
                    continue
                entry = item.replace('&gt;', '>').replace('&lt;', '<')
                base_song = re.split(r'->|&gt;|>', entry)[0].strip()
                if base_song:
                    songs.append(base_song)

    return songs


def get_audio_files_with_global_tracks(show_id):
    """
    Get audio files from archive.org metadata with global track numbers.
    Handles multiple formats per track (MP3, OGG, etc.) by deduplicating
    on (disc, track) and preferring longer durations.
    """
    url = f"{BASE_URL}/metadata/{show_id}"
    resp = requests.get(url, headers={"User-Agent": "GD-EP-Extractor/4.0"}, timeout=30)

    if resp.status_code != 200:
        return None, None

    meta = resp.json()
    files = meta.get('files', [])
    desc = meta.get('metadata', {}).get('description', '')

    audio_files = [f for f in files
                   if f.get('format') in ['FLAC', 'VBR MP3', 'Ogg Vorbis', 'Shorten', 'WAV', 'MP3']
                   and not f.get('name', '').endswith(('.txt', '.torrent'))
                   and 'archive.torrent' not in f.get('name', '')]

    def extract_disc_track(f):
        """Extract (disc, track) from filename, handling both d1t01 and s1t01 patterns."""
        name = f.get('name', '')
        # Try d{N}t{N} pattern first (e.g., gd1977-05-07d2t03.mp3)
        m = re.search(r'd(\d+)t(\d+)', name)
        if m:
            return int(m.group(1)), int(m.group(2))
        # Try s{N}t{N} pattern (e.g., gd1977-05-28s1t01.mp3)
        m = re.search(r's(\d+)t(\d+)', name)
        if m:
            return int(m.group(1)), int(m.group(2))
        # Try t{N} pattern (e.g., gd1977-05-07t03.mp3)
        m = re.search(r't(\d+)', name)
        if m:
            return 1, int(m.group(1))
        # Fallback: track number from sequence
        return 1, 9999

    audio_files.sort(key=lambda x: (extract_disc_track(x), x.get('name', '')))

    file_durations = {}
    global_track = 0
    prev_key = None
    for f in audio_files:
        disc, track_in_disc = extract_disc_track(f)
        key = (disc, track_in_disc)
        if key != prev_key:
            global_track += 1
            prev_key = key
            dur = f.get('length')
            dur_sec = duration_to_seconds(dur) if dur else None
            file_durations[global_track] = {
                'duration_seconds': dur_sec,
                'duration_display': dur,
                'file_name': f.get('name', ''),
                'track_num': global_track,
                'disc': disc,
                'track_in_disc': track_in_disc
            }
        else:
            existing = file_durations[global_track]
            dur = f.get('length')
            dur_sec = duration_to_seconds(dur) if dur else None
            if dur_sec is not None and (existing['duration_seconds'] is None or dur_sec > existing['duration_seconds']):
                file_durations[global_track]['duration_seconds'] = dur_sec
                file_durations[global_track]['duration_display'] = dur
                file_durations[global_track]['file_name'] = f.get('name', '')

    setlist_songs = parse_setlist_for_songs(desc)
    return file_durations, setlist_songs


def get_ep_duration_from_ia_titles(show_id, target_song="estimated prophet"):
    """
    Get a song's duration by matching against IA file metadata 'title' field.

    IA audio files often have a 'title' field containing the song name
    (e.g., "Estimated Prophet", "Tuning", "Drums ->").

    Returns: dict with duration_seconds, duration_display, track_file, source
    or None if not found.
    """
    url = f"{BASE_URL}/metadata/{show_id}"
    resp = requests.get(url, headers={"User-Agent": "GD-EP-Extractor/4.0"}, timeout=30)

    if resp.status_code != 200:
        return None

    meta = resp.json()
    files = meta.get('files', [])

    audio_files = []
    for f in files:
        fmt = f.get('format', '')
        if fmt not in ['FLAC', 'VBR MP3', 'Ogg Vorbis', 'Shorten', 'WAV', 'MP3']:
            continue
        if f.get('name', '').endswith(('.txt', '.torrent')):
            continue
        if 'archive.torrent' in f.get('name', ''):
            continue

        title = f.get('title', '').strip()
        name = f.get('name', '')
        length = f.get('length')

        if not title:
            continue

        dur_sec = duration_to_seconds(length) if length else None

        audio_files.append({
            'name': name,
            'title': title,
            'length': length,
            'duration_seconds': dur_sec,
            'format': fmt
        })

    target_clean = target_song.lower()
    best_match = None

    for f in audio_files:
        title_clean = f['title'].lower().replace('->', '').replace('>', '').strip()
        if title_clean == target_clean:
            best_match = f
            break
        if target_clean in title_clean:
            best_match = f
            break

    if best_match and best_match['duration_seconds']:
        return {
            'duration_seconds': best_match['duration_seconds'],
            'duration_display': best_match['length'],
            'track_file': best_match['name'],
            'source': 'ia_title_match'
        }

    # Fallback: DeadTracks
    result = get_ep_duration_via_deadtracks(show_id, target_song)
    if result:
        return result

    # Last resort: positional matching
    result = get_ep_duration_positional(show_id, target_song)
    if result:
        return result

    return None


def get_ep_duration_via_deadtracks(show_id, target_song="estimated prophet"):
    """Fallback using DeadTracks.com track listing when IA file titles don't match."""
    if target_song.lower() != 'estimated prophet':
        return None

    dt_map = get_deadtracks_track_listing(show_id, EP_SONG_ID)
    if not dt_map:
        return None

    file_durations, _ = get_audio_files_with_global_tracks(show_id)
    if not file_durations:
        return None

    # Try direct track number match
    for track_num, song_name in dt_map.items():
        if song_name.lower() == 'estimated prophet':
            dur_info = file_durations.get(track_num)
            if dur_info and dur_info['duration_seconds']:
                return {
                    'duration_seconds': dur_info['duration_seconds'],
                    'duration_display': dur_info['duration_display'],
                    'track_file': dur_info['file_name'],
                    'source': 'deadtracks_direct'
                }

    # Try positional (filter non-song from both sides)
    deadtracks_songs = []
    for track_num, song_name in sorted(dt_map.items()):
        if any(nonsong in song_name.lower() for nonsong in NON_SONG_TRACKS):
            continue
        deadtracks_songs.append(song_name)

    ia_files = sorted(file_durations.values(), key=lambda x: x['track_num'])
    for i, song_name in enumerate(deadtracks_songs):
        if song_name.lower() == target_song and i < len(ia_files):
            dur_info = ia_files[i]
            if dur_info['duration_seconds']:
                return {
                    'duration_seconds': dur_info['duration_seconds'],
                    'duration_display': dur_info['duration_display'],
                    'track_file': dur_info['file_name'],
                    'source': 'deadtracks_positional'
                }

    return None


def get_ep_duration_positional(show_id, target_song="estimated prophet"):
    """Last-resort fallback: positional matching using setlist order."""
    file_durations, setlist_songs = get_audio_files_with_global_tracks(show_id)

    if not file_durations or not setlist_songs:
        return None

    ep_idx = None
    for idx, song_name in enumerate(setlist_songs):
        if song_name.lower() == target_song.lower():
            ep_idx = idx
            break

    if ep_idx is None:
        return None

    audio_files = sorted(file_durations.values(), key=lambda x: x['track_num'])

    if ep_idx < len(audio_files):
        dur_info = audio_files[ep_idx]
        if dur_info['duration_seconds']:
            return {
                'duration_seconds': dur_info['duration_seconds'],
                'duration_display': dur_info['duration_display'],
                'track_file': dur_info['file_name'],
                'source': 'positional_fallback'
            }

    if len(audio_files) > len(setlist_songs):
        filtered = [f for f in audio_files
                    if f['duration_seconds'] is not None and f['duration_seconds'] >= 120]
        if ep_idx < len(filtered):
            dur_info = filtered[ep_idx]
            if dur_info['duration_seconds']:
                return {
                    'duration_seconds': dur_info['duration_seconds'],
                    'duration_display': dur_info['duration_display'],
                    'track_file': dur_info['file_name'],
                    'source': 'positional_filtered'
                }

    return None


def find_deadtracks_recording_id(show_id, song_id=EP_SONG_ID):
    """Find the DeadTracks recording_id for a given IA show identifier."""
    url = f"{DEADTRACKS_BASE}/songs/{song_id}"
    resp = requests.get(url, headers={"User-Agent": "GD-EP-Extractor/4.0"}, timeout=30)

    if resp.status_code != 200:
        return None

    pattern = rf'href="(/songs/{song_id}/recordings/(\d+))"[^>]*>([^<]+)</a>'

    for m in re.finditer(pattern, resp.text):
        rec_id = int(m.group(2))
        ia_identifier = m.group(3).strip()

        if ia_identifier == show_id:
            return rec_id

    return None


def get_deadtracks_track_listing(show_id, song_id=EP_SONG_ID):
    """Fetch DeadTracks recording page for a show to get track-by-track listing."""
    rec_id = find_deadtracks_recording_id(show_id, song_id)
    if not rec_id:
        return None

    url = f"{DEADTRACKS_BASE}/songs/{song_id}/recordings/{rec_id}"
    resp = requests.get(url, headers={"User-Agent": "GD-EP-Extractor/4.0"}, timeout=30)

    if resp.status_code != 200:
        return None

    return parse_deadtracks_track_table(resp.text)


def parse_deadtracks_track_table(html_text):
    """Parse DeadTracks HTML recording page to extract track -> song mapping."""
    track_map = {}

    tables = re.findall(r'<table[^>]*>(.*?)</table>', html_text, re.DOTALL)

    for table in tables:
        rows = re.findall(r'<tr[^>]*>(.*?)</tr>', table, re.DOTALL)
        for row in rows:
            cells = re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', row, re.DOTALL)
            cleaned = [re.sub(r'<[^>]+>', '', c).strip() for c in cells]

            if len(cleaned) == 2:
                try:
                    track_num = int(cleaned[0])
                    if track_num >= 1:
                        title = unescape(cleaned[1])
                        title = title.replace('>', '').replace('&gt;', '').strip()
                        track_map[track_num] = title
                except ValueError:
                    pass

    return track_map if track_map else None


def find_ep_shows():
    """Find shows with Estimated Prophet in setlists using archive.org search."""
    shows = []
    page = 1

    search_url = f"{BASE_URL}/search.php"

    while True:
        params = {
            'and[]': ['collection:GratefulDead', 'description:"Estimated Prophet"'],
            'rows': 100,
            'page': page,
            'fl[]': ['identifier', 'title', 'date'],
            'sort[]': ['addeddate+desc']
        }

        resp = requests.get(search_url, params=params,
                            headers={"User-Agent": "GD-EP-Extractor/4.0"}, timeout=30)

        if resp.status_code != 200:
            break

        data = resp.json() if resp.headers.get('Content-Type', '').startswith('application/json') else None

        if not data:
            try:
                content = resp.text
                json_match = re.search(r'JSON\.parse\(escapeJavaScript\("(.*?)"\)\)', content)
                if json_match:
                    data = json.loads(json_match.group(1).replace('\\"', '"'))
            except Exception:
                break

        if not data or 'response' not in data or 'docs' not in data['response']:
            break

        docs = data['response']['docs']
        if not docs:
            break

        for doc in docs:
            shows.append({
                'id': doc.get('identifier'),
                'title': doc.get('title', ''),
                'date': doc.get('date', '')
            })

        print(f"Page {page}: Found {len(docs)} shows. Total: {len(shows)}")
        page += 1

        if len(docs) < 100:
            break

        time.sleep(1.5)

    return shows


if __name__ == "__main__":
    output_file = Path("/home/mao/DaveMatt/gd-project/data/ep_durations.json")
    combined_file = Path("/home/mao/DaveMatt/gd-project/data/gd_comments_combined.json")

    if combined_file.exists():
        with open(combined_file) as f:
            data = json.load(f)
        setlists = data.get('setlists', {})

        ep_shows = []
        for sid, sl in setlists.items():
            if isinstance(sl, list) and any('estimated prophet' in s.lower() for s in sl):
                ep_shows.append(sid)
            elif isinstance(sl, dict) and any('estimated prophet' in s.lower() for s in sl.get('songs', [])):
                ep_shows.append(sid)

        print(f"Found {len(ep_shows)} shows with Estimated Prophet in setlists")

        results = []
        count = 0
        title_match_count = 0
        deadtracks_count = 0
        fallback_count = 0
        no_match_count = 0

        for show_id in ep_shows:
            count += 1
            if count % 20 == 0:
                print(f"Processed {count}/{len(ep_shows)} shows...")
                print(f"  Title: {title_match_count}, DeadTracks: {deadtracks_count}, "
                       f"Fallback: {fallback_count}, No match: {no_match_count}")

            ep_dur = get_ep_duration_from_ia_titles(show_id)
            if ep_dur:
                if ep_dur.get('source') == 'ia_title_match':
                    title_match_count += 1
                elif 'deadtracks' in ep_dur.get('source', ''):
                    deadtracks_count += 1
                else:
                    fallback_count += 1

                results.append({
                    'show_id': show_id,
                    'song': 'Estimated Prophet',
                    'duration_seconds': ep_dur['duration_seconds'],
                    'duration_display': ep_dur['duration_display'],
                    'track_file': ep_dur['track_file'],
                    'source': ep_dur.get('source', 'unknown')
                })
            else:
                no_match_count += 1

            time.sleep(DELAY)

        # Sort by duration (descending)
        results.sort(key=lambda x: x['duration_seconds'] or 0, reverse=True)

        # Save results
        with open(output_file, 'w') as f:
            json.dump(results, f, indent=2)

        # Print top 20
        print("\n=== Top 20 Longest Estimated Prophet Performances ===")
        print(f"{'Rank':<5} {'Show':<55} {'Dur':<10} {'Sec':<10} {'Track':<25} {'Source'}")
        print("-" * 120)

        for i, r in enumerate(results[:20], 1):
            dur_sec = r['duration_seconds']
            if dur_sec:
                dur_str = f"{int(dur_sec)//60}:{int(dur_sec)%60:02d}"
            else:
                dur_str = "N/A"
            track = r.get('track_file', '')[:25]
            source = r.get('source', '')
            print(f"{i:<5} {r['show_id']:<55} {dur_str:<10} {dur_sec:<10} {track:<25} {source}")

        print(f"\nTotal Estimated Prophet performances found: {len(results)}")
        print(f"  IA title matches: {title_match_count}")
        print(f"  DeadTracks matches: {deadtracks_count}")
        print(f"  Positional fallbacks: {fallback_count}")
        print(f"  No match: {no_match_count}")
        print(f"Results saved to: {output_file}")

        # Save the top 10 to a markdown file
        md_file = Path("/home/mao/DaveMatt/gd-project/output/top_10_estimated_prophet.md")
        md_file.parent.mkdir(parents=True, exist_ok=True)

        with open(md_file, 'w') as f:
            f.write("# Top 10 Longest Estimated Prophet Performances\n\n")
            f.write(f"Based on analysis of {len(results)} performances across {len(ep_shows)} shows.\n\n")
            f.write("| Rank | Show ID | Duration | Track File | Source |\n")
            f.write("|------|---------|----------|------------|--------|\n")

            for i, r in enumerate(results[:10], 1):
                dur_sec = r['duration_seconds']
                if dur_sec:
                    dur_str = f"{int(dur_sec)//60}:{int(dur_sec)%60:02d}"
                else:
                    dur_str = "N/A"
                f.write(f"| {i} | {r['show_id']} | {dur_str} | {r['track_file']} | {r.get('source', '')} |\n")

            f.write(f"\n*Total performances found: {len(results)}*\n")
            f.write(f"\n- IA title matches: {title_match_count}\n")
            f.write(f"- DeadTracks matches: {deadtracks_count}\n")
            f.write(f"- Positional fallbacks: {fallback_count}\n")
            f.write(f"- No match: {no_match_count}\n")

        print(f"\nTop 10 list saved to: {md_file}")