#!/usr/bin/env python3
"""
GD RAT Query Interface with LLM Summarization

Retrieves relevant comments, setlists, transitions, and deadcast transcripts
from the vector index, then uses an LLM (OpenAI or local extractive summary)
to synthesize a curated answer with citations to specific shows.

Usage:
    python3 query_rat.py "question about the Grateful Dead"
    python3 query_rat.py          (interactive mode)
    OPENAI_API_KEY=... python3 query_rat.py "question"

Enhanced query prefixes:
    song:"Estimated Prophet"  - Search setlist entries for this song
    guest:"Duane Allman"      - Search for guest artist performances
    transition:"Eyes -> Space" - Search for song transitions
    deadcast:"Jerry"          - Search Deadcast podcast transcripts
    ep:                       - Search for Estimated Prophet durations
"""

import json
import os
import re
import sys

import faiss
import numpy as np
from sentence_transformers import SentenceTransformer

INDEX_DIR = os.path.join(os.path.dirname(__file__), "..", "index")
INDEX_PATH = os.path.join(INDEX_DIR, "vector_index.faiss")
META_PATH = os.path.join(INDEX_DIR, "index_metadata.json")


# ---------------------------------------------------------------
# Index + model loading
# ---------------------------------------------------------------
def load_index():
    """Load FAISS index, metadata, and embedding model."""
    index = faiss.read_index(INDEX_PATH)
    with open(META_PATH, "r", encoding="utf-8") as f:
        raw = json.load(f)

    metadata = raw.get("comments", []) if isinstance(raw, dict) else raw
    setlists = raw.get("setlists", {}) if isinstance(raw, dict) else {}
    deadcast_transcripts = raw.get("deadcast_transcripts", []) if isinstance(raw, dict) else []
    ep_durations = raw.get("ep_durations", []) if isinstance(raw, dict) else []

    model = SentenceTransformer("all-MiniLM-L6-v2")
    return index, metadata, setlists, deadcast_transcripts, ep_durations, model


# ---------------------------------------------------------------
# Vector search
# ---------------------------------------------------------------
def search(index, metadata, model, query: str, k: int = 20) -> list[dict]:
    """Search FAISS index for top-k most relevant entries."""
    q_vec = model.encode([query], convert_to_numpy=True)
    distances, indices = index.search(q_vec, k)

    results = []
    for dist, idx in zip(distances[0], indices[0]):
        if idx < 0 or idx >= len(metadata):
            continue
        results.append({"score": float(dist), "meta": metadata[idx]})
    return results


def format_show_url(show_id: str) -> str:
    if not show_id:
        return ""
    return f"https://archive.org/details/{show_id}"


# ---------------------------------------------------------------
# Enhanced search helpers
# ---------------------------------------------------------------
def search_songs(metadata: list[dict], song_name: str, k: int = 20,
                 setlists: dict = None) -> list[dict]:
    """Search for specific song performances in setlists.

    Also searches the setlists dict directly for shows containing the song,
    then returns those as results with duration info.
    """
    matches = []
    song_lower = song_name.lower()

    # First: search metadata (FAISS index entries)
    for m in metadata:
        if m.get("type") == "setlist_song" and song_lower in m.get("song_name", "").lower():
            matches.append({"score": 0.0, "meta": m})

    # If we have setlists dict, also search there for additional shows
    if setlists:
        for show_id, sl in setlists.items():
            if not isinstance(sl, dict):
                continue
            songs = sl.get("songs", [])
            song_names = [s.get("name", "").lower() for s in songs if isinstance(s, dict)]
            for s in songs:
                if isinstance(s, dict):
                    if song_lower in s.get("name", "").lower():
                        matches.append({
                            "score": 0.0,
                            "meta": {
                                "type": "setlist_song",
                                "show_identifier": show_id,
                                "song_name": s.get("name", ""),
                                "duration": s.get("duration", ""),
                                "track_file": s.get("track_file", ""),
                                "comment_text": f"[Setlist] {s.get('name', '')} performed at {show_id}",
                                "created": sl.get("date", ""),
                            }
                        })

    return matches[:k]


def search_guests(metadata: list[dict], guest_name: str, k: int = 10) -> list[dict]:
    """Search for guest artist performances."""
    matches = []
    for m in metadata:
        if m.get("type") == "guest_artist" and guest_name.lower() in m.get("guest_artist", "").lower():
            matches.append({"score": 0.0, "meta": m})
    return matches[:k]


def search_transitions(metadata: list[dict], from_song: str, to_song: str = None, k: int = 10) -> list[dict]:
    """Search for song transitions."""
    matches = []
    for m in metadata:
        if m.get("type") == "transition":
            from_match = from_song.lower() in m.get("transition_from", "").lower()
            to_match = to_song is None or to_song.lower() in m.get("transition_to", "").lower()
            if from_match and to_match:
                matches.append({"score": 0.0, "meta": m})
    return matches[:k]


def search_deadcast(deadcast: list[dict], query: str, k: int = 10) -> list[dict]:
    """Search Deadcast podcast transcripts for a query term."""
    query_lower = query.lower()
    matches = []
    for dt in deadcast:
        title = dt.get("title", "")
        transcript = dt.get("transcript", "")
        if query_lower in title.lower() or query_lower in transcript.lower():
            # Find context around matches
            context = ""
            for field in [title, transcript]:
                idx = field.lower().find(query_lower)
                if idx >= 0:
                    start = max(0, idx - 100)
                    end = min(len(field), idx + 300)
                    context = field[start:end]
                    break

            text = transcript[:500] if transcript else ""
            matches.append({
                "score": 0.0,
                "meta": {
                    "type": "deadcast_transcript",
                    "title": title,
                    "url": dt.get("url", ""),
                    "word_count": dt.get("word_count", 0),
                    "comment_text": f"[Deadcast] {title}",
                    "context": context,
                    "full_text": text,
                }
            })
    return matches[:k]


def search_ep_durations(ep_durations=None, k=20):
    """Load and rank Estimated Prophet durations from ep_durations.json."""
    if ep_durations is None:
        ep_path = os.path.join(
            os.path.dirname(__file__), "data", "ep_durations.json"
        )
        if not os.path.exists(ep_path):
            return []
        with open(ep_path) as f:
            ep_data = json.load(f)
    else:
        ep_data = ep_durations

    # Known GD venues for common show dates (from DeadTracks community database)
    # Used as fallback when venue isn't in metadata
    known_venues = {
        "1978-09-02": "Red Rocks Amphitheatre, Morrison, CO",
        "1984-04-13": "Crisler Arena, Ann Arbor, MI",
        "1985-11-11": "Cope Arena, Orlando, FL",
        "1982-07-25": "Oakland Coliseum, Oakland, CA",
        "1978-01-30": "Shrine Auditorium, Los Angeles, CA",
        "1980-12-31": "San Francisco Civic Center, San Francisco, CA",
        "1983-04-13": "Broome County Veterans Memorial Arena, Binghamton, NY",
        "1977-03-18": "Winterland Arena, San Francisco, CA",
        "1985-08-31": "Universal Amphitheatre, Los Angeles, CA",
        "1977-05-07": "Boston Garden, Boston, MA",
        "1977-05-08": "Boston Garden, Boston, MA",
        "1977-05-09": "Boston Garden, Boston, MA",
        "1977-05-17": "Hollywood Palladium, Los Angeles, CA",
    }

    # Sort by duration descending
    sorted_data = sorted(
        [d for d in ep_data if d.get("duration_seconds") is not None],
        key=lambda x: x["duration_seconds"],
        reverse=True
    )

    results = []
    for entry in sorted_data[:k]:
        show_id = entry.get("show_id", "")
        dur_sec = entry.get("duration_seconds", 0)
        dur_str = entry.get("duration_display", "")

        # Try to get venue from entry, or known_venues by date
        venue = entry.get("venue", "")
        if not venue:
            date_match = re.search(r'gd(\d{4}-\d{2}-\d{2})', show_id)
            if date_match:
                date_str = date_match.group(1)
                venue = known_venues.get(date_str, "Unknown venue")

        results.append({
            "score": 0.0,
            "meta": {
                "type": "ep_duration",
                "show_identifier": show_id,
                "song_name": entry.get("song", "Estimated Prophet"),
                "duration": dur_str,
                "duration_formatted": f"{int(dur_sec)//60}:{int(dur_sec)%60:02d}",
                "duration_seconds": dur_sec,
                "track_file": entry.get("track_file", ""),
                "source": entry.get("source", ""),
                "venue": venue,
                "comment_text": f"[EP Duration] Estimated Prophet: {int(dur_sec)//60}:{int(dur_sec)%60:02d} ({dur_sec}s) at {show_id}",
                "created": "",
            }
        })

    return results


# ---------------------------------------------------------------
# LLM summarization
# ---------------------------------------------------------------
def _format_result_line(i, m):
    """Format a single result entry for the LLM prompt or extractive summary."""
    mtype = m.get("type", "comment")
    show_id = m.get("show_identifier", "")
    show_url = format_show_url(show_id) if show_id else ""
    text = m.get("comment_text", "")
    if len(text) > 500:
        text = text[:500] + "..."

    if mtype == "comment":
        return (f"[{i}] Show: {show_id} {f'({show_url})' if show_url else ''} "
                f"(rating: {m.get('rating','N/A')}/5, date: {m.get('created','')})\n"
                f"Comment: {text}")
    elif mtype == "setlist_song":
        dur = m.get("duration", "")
        dur_str = f" (duration: {dur})" if dur else ""
        return (f"[{i}] Show: {show_id} {f'({show_url})' if show_url else ''} "
                f"(song: {m.get('song_name','')}{dur_str}, date: {m.get('created','')})\n"
                f"Setlist entry: {text}")
    elif mtype == "guest_artist":
        return (f"[{i}] Show: {show_id} {f'({show_url})' if show_url else ''} "
                f"(guest: {m.get('guest_artist','')}, date: {m.get('created','')})\n"
                f"{text}")
    elif mtype == "transition":
        return (f"[{i}] Show: {show_id} {f'({show_url})' if show_url else ''} "
                f"(transition: {m.get('transition_from','')} -> {m.get('transition_to','')}, "
                f"date: {m.get('created','')})\n"
                f"{text}")
    elif mtype == "deadcast_transcript":
        title = m.get("title", "")
        url = m.get("url", "")
        return (f"[{i}] Deadcast: {title} ({m.get('word_count', 0)} words)\n"
                f"URL: {url}\n"
                f"Context: {m.get('context', text)}")
    elif mtype == "ep_duration":
        venue = m.get("venue", "")
        dur_fmt = m.get("duration_formatted", m.get("duration", ""))
        venue_str = f" | Venue: {venue}" if venue else ""
        return (f"[{i}] EP Duration: {dur_fmt} ({m.get('duration_seconds', 0)}s)\n"
                f"Show: {m.get('show_identifier', '')} ({format_show_url(m.get('show_identifier', ''))})\n"
                f"Track: {m.get('track_file', '')}{venue_str}\n"
                f"Date: {m.get('created', 'unknown')}")

    return f"[{i}] {text}"


def summarize_with_openai(query: str, results: list[dict], api_key: str) -> str:
    from openai import OpenAI
    client = OpenAI(api_key=api_key)

    context_parts = [_format_result_line(i, r["meta"]) for i, r in enumerate(results, 1)]
    context = "\n\n".join(context_parts)

    prompt = f"""You are "Hal," a Grateful Dead knowledge bot. The user asked: "{query}"

Below are {len(results)} relevant results from the GD archive (comments, setlist entries,
guest info, transitions, deadcast transcripts, EP durations). Synthesize a helpful,
well-structured answer that:
1. Summarizes the key points
2. Cites specific shows with their archive.org links
3. Includes quoted excerpts where useful
4. Stays grounded in the provided data — do NOT invent facts

Source material:
{context}
"""

    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
        max_tokens=1500,
    )
    return resp.choices[0].message.content.strip()


def summarize_with_local(query: str, results: list[dict]) -> str:
    """Fall back to a simple extractive summary if no OpenAI key."""
    lines = [f"**Based on {len(results)} sources:**\n"]
    for i, r in enumerate(results, 1):
        m = r["meta"]
        lines.append(_format_result_line(i, m) + "\n")

    lines.append("---\n")
    lines.append("*No OpenAI API key set. For synthesized answers, set OPENAI_API_KEY.*\n")
    return "\n".join(lines)


# ---------------------------------------------------------------
# Main respond function
# ---------------------------------------------------------------
def respond(query: str, results: list[dict]) -> str:
    api_key = os.environ.get("OPENAI_API_KEY")
    if api_key:
        try:
            return summarize_with_openai(query, results, api_key)
        except Exception as e:
            header = f"⚠️ LLM API error: {e}. Using extractive summary:\n\n"
            return header + summarize_with_local(query, results)
    else:
        return summarize_with_local(query, results)


# ---------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------
def main():
    query = sys.argv[1] if len(sys.argv) > 1 else None

    index, metadata, setlists, deadcast_transcripts, ep_durations, model = load_index()
    print(f"Loaded index: {len(metadata)} entries, {len(setlists)} setlists, "
          f"{len(deadcast_transcripts)} deadcast transcripts, {len(ep_durations)} EP durations", file=sys.stderr)

    if not query:
        print("GD RAT Chatbot (type 'quit' to exit)")
        print("Enhanced queries: song:\"name\" guest:\"artist\" transition:\"from -> to\" deadcast:\"term\" ep:")
        while True:
            query = input("You: ").strip()
            if query.lower() in ("quit", "exit", "q"):
                break
            if not query:
                continue

            results = _handle_enhanced_query(query, index, metadata, setlists,
                                             deadcast_transcripts, ep_durations, model)
            if results is None:
                continue
            if isinstance(results, str):
                print(f"\n{results}\n")
            else:
                print(f"\n{respond(query, results)}\n")
        return

    results = _handle_enhanced_query(query, index, metadata, setlists,
                                     deadcast_transcripts, ep_durations, model)
    if isinstance(results, str):
        print(results)
    elif results:
        print(respond(query, results))
    else:
        print("No results found.")


def _handle_enhanced_query(query, index, metadata, setlists, deadcast_transcripts, ep_durations, model):
    """Parse enhanced query prefixes and dispatch to appropriate search."""
    if query.startswith("song:"):
        song = query[6:].strip().strip('"')
        return search_songs(metadata, song, k=20, setlists=setlists)
    elif query.startswith("guest:"):
        guest = query[7:].strip().strip('"')
        return search_guests(metadata, guest, k=10)
    elif query.startswith("transition:"):
        trans = query[12:].strip().strip('"')
        parts = trans.split("->")
        if len(parts) == 2:
            return search_transitions(metadata, parts[0].strip(), parts[1].strip(), k=10)
        else:
            return search_transitions(metadata, trans, k=10)
    elif query.startswith("deadcast:"):
        term = query[10:].strip().strip('"')
        return search_deadcast(deadcast_transcripts, term, k=10)
    elif query.startswith("ep:"):
        # Show top EP durations
        return search_ep_durations(ep_durations, k=20)
    else:
        return search(index, metadata, model, query, k=20)


if __name__ == "__main__":
    main()
