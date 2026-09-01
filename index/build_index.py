#!/usr/bin/env python3
"""
Build a FAISS vector index from GD comment data for the RAT.

Also embeds setlist data so queries can match songs mentioned in comments
to specific shows' setlists.

Usage:
    python3 build_index.py [data_json_path]

Output:
    - vector_index.faiss    (FAISS index)
    - index_metadata.json    (comment metadata + setlists)
"""

import gc
import json
import os
import re
import sys

import faiss
import numpy as np
from sentence_transformers import SentenceTransformer


DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
INDEX_DIR = os.path.join(os.path.dirname(__file__), "..", "index")


def load_data(data_path: str) -> dict:
    with open(data_path, "r", encoding="utf-8") as f:
        return json.load(f)


def normalize_setlist(show_id: str, sl) -> dict:
    """Normalize a setlist (dict or list format) into a standard dict.

    Handles three formats:
    1. Enhanced dict: {'sets': [...], 'songs': [...], 'transitions': [...], ...}
    2. Legacy list: ['Song A', 'Song B > Song C', ...]
    3. Empty or None
    """
    if not sl or not isinstance(sl, (dict, list)):
        return {}

    if isinstance(sl, dict):
        return sl

    # Legacy list format — convert to dict
    result = {
        'sets': [],
        'songs': [],
        'transitions': [],
        'guest_artists': [],
        'song_durations': {},
        'raw_description': ''
    }

    current_set = None  # Lazily created when we encounter the first song entry

    # Known GD song titles, sorted by length (longest first) for matching
    GD_SONGS = [
        'turn on your lovelight', 'estimated prophet', 'scarlet begonias',
        'sugar magnolia', 'morning dew', 'casey jones', 'mama tried',
        'high time', 'easy wind', 'yellow dog story', 'dark star',
        'saint stephen', 'the eleven', 'truckin', 'good lovin',
        'playing in the band', 'wharf rat', 'me and my uncle',
        'friend of the devil', 'big railroad', 'cumberland gorge',
        'me and bobby magee', 'row jimmy', 'brokedown servants',
        'greatest story ever', 'terrapin station', 'the other one',
        'stella blue', 'help on the way', 'slipknot', 'franklin tower',
        'promised land', 'black peter', 'one more saturday night',
        'lost sailor', 'st of cdc', 'china cat sunflower',
        'i know you rider', 'around here', 'space', 'drumz',
        'one more saturday', 'sugar', 'bertha', 'sunshine',
        'eyes', 'wheel', 'dark star',
    ]

    def _split_songs(text: str) -> list:
        """Split a song-list string into individual song names using known titles as anchors."""
        text_lower = text.lower().strip()
        if not text_lower:
            return []
        # Sort by length so longest titles match first (avoids "Sugar" matching inside "Sugar Magnolia")
        for song in sorted(GD_SONGS, key=len, reverse=True):
            pattern = song
            idx = text_lower.find(pattern)
            if idx >= 0 and (idx == 0 or text_lower[idx-1] == ' '):
                end = idx + len(song)
                if end == len(text_lower) or text_lower[end] in (' ', '>', '&'):
                    # Split into left and right parts
                    left = text[:idx].strip()
                    right = text[end:].strip()
                    left_songs = _split_songs(left)
                    right_songs = _split_songs(right)
                    return left_songs + [song.title()] + right_songs
        # No known song found — return the text as-is (single entry)
        return [text.strip()] if text.strip() else []

    def _parse_song_entry(entry_text: str) -> list:
        """Parse a song entry that may contain '>' transitions.
        Returns list of dicts: [{'name': 'SongA', 'transition': False}, ...]
        """
        entry_text = entry_text.replace('&gt;', '>').strip()
        # First split on '>' to get segments
        segments = entry_text.split('>')
        segments = [s.strip() for s in segments if s.strip()]
        songs = []
        for i, seg in enumerate(segments):
            song_parts = _split_songs(seg)
            for song_name in song_parts:
                song_name = song_name.strip()
                if not song_name:
                    continue
                is_transition = (i > 0)
                songs.append({
                    'name': song_name,
                    'transition': is_transition,
                })
                if i > 0:
                    prev_name = songs[-2]['name'] if len(songs) >= 2 else ''
                    if prev_name:
                        result['transitions'].append({
                            'from': prev_name,
                            'to': song_name,
                        })
        return songs

    for entry in sl:
        if isinstance(entry, str):
            entry_clean = entry.replace('&gt;', '>').strip()

            # Check if this is a set marker (e.g., "Set I", "Set II", "Encore")
            set_pattern = re.match(r'^(Set\s+[IVX]+|Disc\s+\d+|Encore)\s*(.*)', entry_clean, re.IGNORECASE)
            if set_pattern:
                set_name = set_pattern.group(1)
                rest = set_pattern.group(2).strip()
                current_set = {
                    'name': set_name,
                    'order': len(result['sets']),
                    'songs': []
                }
                result['sets'].append(current_set)
                if not result['raw_description']:
                    result['raw_description'] = entry_clean
                # Process songs after the set marker on the same line
                if rest:
                    parsed_songs = _parse_song_entry(rest)
                    for s in parsed_songs:
                        current_set['songs'].append(s)
                        result['songs'].append(s['name'])
                continue

            # Check if this is a musician info line
            if ' - ' in entry_clean and any(x in entry_clean.lower() for x in ['guitar', 'bass', 'drums', 'keyboards']):
                if current_set is None:
                    current_set = {'name': 'Set I', 'order': 0, 'songs': []}
                    result['sets'].append(current_set)
                current_set['musicians'] = entry_clean
                result['raw_description'] = entry_clean
                continue

            # Parse as song entries
            if current_set is None:
                current_set = {'name': 'Set I', 'order': 0, 'songs': []}
                result['sets'].append(current_set)
            try:
                parsed = _parse_song_entry(entry_clean)
                for s in parsed:
                    current_set['songs'].append(s)
                    result['songs'].append(s['name'])
            except Exception:
                pass

    return result


def build_index(comments: list[dict], setlists: dict, deadcast_transcripts: list = None) -> tuple:
    """Embed all comment texts and build a FAISS flat L2 index.

    Also embeds setlist metadata (songs, guests, transitions, durations) so
    queries can match across comments *and* structured setlist data.

    Uses a memory-efficient approach: small batch size, and processes
    setlist embeddings separately from comment embeddings to avoid OOM.
    """
    # Use smaller model for low-memory environments
    model = SentenceTransformer("all-MiniLM-L6-v2")

    texts = []
    metadata = []

    # --- 1. Embed comments (original behavior) ---
    for i, c in enumerate(comments):
        text = c.get("comment_text", "").strip()
        if not text:
            continue
        texts.append(text)
        metadata.append({
            "idx": len(metadata),
            "show_identifier": c.get("show_identifier", ""),
            "reviewer": c.get("reviewer", "") or "anonymous",
            "rating": c.get("rating", ""),
            "created": c.get("created", ""),
            "comment_text": text,
            "type": "comment",
        })

    # --- 2. Embed setlist entries (enhanced) ---
    for show_id, sl_raw in setlists.items():
        sl = normalize_setlist(show_id, sl_raw)
        if not sl or not sl.get('songs'):
            continue

        # Collect unique song names from this show's setlist
        seen_songs = set()

        # Get song names from sets structure (with duration info)
        set_songs = []
        for s_set in sl.get("sets", []):
            for se in s_set.get('songs', []):
                song_name = se.get('name', '').strip()
                if song_name and song_name not in seen_songs:
                    seen_songs.add(song_name)
                    duration = se.get('duration', '')
                    set_songs.append({
                        'name': song_name,
                        'duration': duration
                    })

        # Also add songs from flat list that weren't in sets
        for song in sl.get("songs", []):
            song_name = song.get("name", "") if isinstance(song, dict) else str(song)
            song_name = song_name.strip()
            if song_name and song_name not in seen_songs:
                seen_songs.add(song_name)
                set_songs.append({'name': song_name, 'duration': ''})

        # Embed each unique song mention
        for song_info in set_songs:
            song_name = song_info['name']
            texts.append(f"{song_name} - Grateful Dead setlist")
            meta_entry = {
                "idx": len(metadata),
                "show_identifier": show_id,
                "song_name": song_name,
                "rating": sl.get("rating", ""),
                "created": sl.get("date", ""),
                "comment_text": f"[Setlist] {song_name} performed at {show_id}",
                "type": "setlist_song",
            }
            if song_info.get('duration'):
                meta_entry["duration"] = song_info['duration']
            metadata.append(meta_entry)

        # Embed guest artists
        for guest in sl.get("guest_artists", []):
            if not guest:
                continue
            texts.append(f"{guest} played with Grateful Dead at {show_id}")
            metadata.append({
                "idx": len(metadata),
                "show_identifier": show_id,
                "guest_artist": guest,
                "created": sl.get("date", ""),
                "comment_text": f"[Guest] {guest} performed with Grateful Dead",
                "type": "guest_artist",
            })

        # Embed transitions
        for trans in sl.get("transitions", []):
            from_song = trans.get("from", "")
            to_song = trans.get("to", "")
            if from_song and to_song:
                texts.append(f"Transition: {from_song} -> {to_song}")
                metadata.append({
                    "idx": len(metadata),
                    "show_identifier": show_id,
                    "transition_from": from_song,
                    "transition_to": to_song,
                    "created": sl.get("date", ""),
                    "comment_text": f"[Transition] {from_song} transitioned into {to_song}",
                    "type": "transition",
                })

    # --- 3. Embed deadcast transcripts (if available) ---
    if deadcast_transcripts is None:
        deadcast_transcripts = []
    for dt in deadcast_transcripts:
        transcript = dt.get("transcript", "").strip()
        if not transcript:
            continue
        # Split long transcripts into chunks for embedding
        chunk_size = 500  # words per chunk
        words = transcript.split()
        for i in range(0, len(words), chunk_size):
            chunk = " ".join(words[i:i+chunk_size])
            texts.append(chunk)
            metadata.append({
                "idx": len(metadata),
                "show_identifier": "",
                "source": "deadcast",
                "title": dt.get("title", ""),
                "url": dt.get("url", ""),
                "chunk_index": i // chunk_size,
                "word_count": len(chunk.split()),
                "comment_text": f"[Deadcast] {dt.get('title', '')} (chunk {i // chunk_size})",
                "type": "deadcast_transcript",
            })

    if not texts:
        print("WARNING: No texts to embed!")
        return None, [], model

    print(f"  Total texts to embed: {len(texts)}")

    # Use small batch size for memory efficiency — add to index incrementally
    dim = None
    index = None
    batch_size = 32

    for i in range(0, len(texts), batch_size):
        batch = texts[i:i+batch_size]
        emb = model.encode(batch, show_progress_bar=False, convert_to_numpy=True)
        if dim is None:
            dim = emb.shape[1]
            # For large index, use IndexIVFFlat for faster search
            # But for small memory, use flat index
            index = faiss.IndexFlatL2(dim)
        index.add(emb)
        del emb
        if i % 500 == 0:
            gc.collect()
            print(f"  Progress: {i}/{len(texts)} texts embedded")

    gc.collect()
    return index, metadata, model


def main():
    data_path = sys.argv[1] if len(sys.argv) > 1 else \
        os.path.join(DATA_DIR, "gd_comments_combined.json")
    os.makedirs(INDEX_DIR, exist_ok=True)

    print(f"Loading data from {data_path} ...")
    data = load_data(data_path)
    comments = data.get("comments", [])
    setlists = data.get("setlists", {})
    deadcast_transcripts = data.get("deadcast_transcripts", [])

    # Count setlist entries by format
    enhanced_count = 0
    legacy_count = 0
    for sl in setlists.values():
        if isinstance(sl, dict):
            enhanced_count += 1
        elif isinstance(sl, list):
            legacy_count += 1

    print(f"  {len(comments)} comments loaded")
    print(f"  {len(setlists)} setlists loaded")
    print(f"    Enhanced dict format: {enhanced_count}")
    print(f"    Legacy list format: {legacy_count}")

    print("\nBuilding FAISS index (this may take a minute) ...")
    index, metadata, model = build_index(comments, setlists, deadcast_transcripts)
    if index is None:
        print("ERROR: No index built — no data to embed")
        return 1

    print(f"  Index built with {len(metadata)} vectors (dim={index.d})")

    faiss_path = os.path.join(INDEX_DIR, "vector_index.faiss")
    meta_path = os.path.join(INDEX_DIR, "index_metadata.json")

    faiss.write_index(index, faiss_path)
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump({"comments": metadata, "setlists": setlists, "deadcast_transcripts": deadcast_transcripts}, f, indent=2, ensure_ascii=False)

    print(f"\nSaved:")
    print(f"  Index:  {faiss_path}")
    print(f"  Meta:   {meta_path}")
    print(f"  Vectors: {len(metadata)}")

    # Print breakdown by type
    type_counts = {}
    for m in metadata:
        t = m.get('type', 'unknown')
        type_counts[t] = type_counts.get(t, 0) + 1
    print(f"  Breakdown:")
    for t, c in sorted(type_counts.items()):
        print(f"    {t}: {c}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
