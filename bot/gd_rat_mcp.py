#!/usr/bin/env python3
"""
Grateful Dead RAT (Retrieve-Augmented Truthiness) MCP Server — "Lemieux"

Provides tools for querying the GD archive:
- search_comments(query)          — vector search GD comments
- search_songs(song_name)         — find setlist entries for a song
- search_guests(guest_name)       — find guest artist appearances
- search_transitions(from, to)    — find song-to-song segues
- get_top_estimated_prophet()     — top 10 longest Estimated Prophet (with venues)
- search_deadcasts(term)          — search Deadcast podcast transcripts
- ask_gd_bot(query)               — natural-language question with LLM synthesis

Usage as MCP server:
    python3 gd_rat_mcp.py

Standalone:
    python3 gd_rat_mcp.py --query "longest Dark Star"
"""
import json
import os
import sys
import re
import asyncio
from pathlib import Path
from datetime import datetime
from typing import Any, AsyncIterator

from mcp.server import Server, stdio
from mcp.server.models import InitializationOptions
from mcp.types import (
    TextContent,
    ListToolsResult,
    CallToolResult,
    Tool,
)

# Configure paths
PROJECT_DIR = Path("/home/mao/DaveMatt/gd-project")
INDEX_DIR = PROJECT_DIR / "index"
DATA_DIR = PROJECT_DIR / "data"
INDEX_PATH = INDEX_DIR / "vector_index.faiss"
META_PATH = INDEX_DIR / "index_metadata.json"

# Known GD venues for common show dates (from DeadTracks community database)
KNOWN_VENUES = {
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

# Global cache for model/index
_cache = {}


def load_resources():
    """Load the FAISS index, metadata, and embedding model."""
    if 'model_loaded' not in _cache:
        if INDEX_PATH.exists():
            try:
                import faiss
                _cache['index'] = faiss.read_index(str(INDEX_PATH))
                with open(META_PATH) as f:
                    raw = json.load(f)
                if isinstance(raw, list):
                    _cache['metadata'] = raw
                    _cache['setlists'] = {}
                    _cache['ep_durations'] = []
                    _cache['deadcast_transcripts'] = []
                else:
                    _cache['metadata'] = raw.get('comments', [])
                    _cache['setlists'] = raw.get('setlists', {})
                    _cache['ep_durations'] = raw.get('ep_durations', [])
                    _cache['deadcast_transcripts'] = raw.get('deadcast_transcripts', [])
            except ImportError:
                _cache['index'] = None
                _cache['metadata'] = []
                _cache['setlists'] = {}
                _cache['ep_durations'] = []
                _cache['deadcast_transcripts'] = []
        else:
            _cache['index'] = None
            _cache['metadata'] = []
            _cache['setlists'] = {}
            _cache['ep_durations'] = []
            _cache['deadcast_transcripts'] = []

        try:
            from sentence_transformers import SentenceTransformer
            _cache['model'] = SentenceTransformer("all-MiniLM-L6-v2")
        except ImportError:
            _cache['model'] = None

        _cache['model_loaded'] = True


def _build_text_response(text: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text)])


def _format_date(date_str: str) -> str:
    """Convert YYYY-MM-DD to 'Month DD, YYYY'."""
    if not date_str:
        return "unknown"
    try:
        date_obj = datetime.strptime(date_str, "%Y-%m-%d")
        return date_obj.strftime("%B %d, %Y")
    except (ValueError, TypeError):
        return date_str


def _venue_for_date(date_str: str, ep_entry: dict) -> str:
    """Get venue for a show date from known venues or ep_entry."""
    venue = ep_entry.get("venue", "")
    if venue:
        return venue
    if date_str in KNOWN_VENUES:
        return KNOWN_VENUES[date_str]
    return "Unknown venue"


async def handle_list_tools():
    """List all available GD query tools."""
    return ListToolsResult(tools=[
        Tool(
            name="search_comments",
            description="Search Grateful Dead archive comments using vector similarity. Finds relevant reviews, discussions, and audience comments about shows, songs, and performances.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "What to search for (e.g., 'best Dark Star', 'Venom', '1977 spring tour')"},
                    "k": {"type": "number", "description": "Number of results to return (default: 10)", "default": 10}
                },
                "required": ["query"]
            }
        ),
        Tool(
            name="search_songs",
            description="Search for specific song performances in setlists across all shows.",
            inputSchema={
                "type": "object",
                "properties": {
                    "song_name": {"type": "string", "description": "Song name to search for (e.g., 'Estimated Prophet', 'Dark Star')"}
                },
                "required": ["song_name"]
            }
        ),
        Tool(
            name="search_guests",
            description="Search for guest artist appearances with the Grateful Dead.",
            inputSchema={
                "type": "object",
                "properties": {
                    "guest_name": {"type": "string", "description": "Guest artist name (e.g., 'Duane Allman', 'Branford Marsalis')"}
                },
                "required": ["guest_name"]
            }
        ),
        Tool(
            name="search_transitions",
            description="Search for song transitions (when one song flows into another).",
            inputSchema={
                "type": "object",
                "properties": {
                    "from_song": {"type": "string", "description": "Starting song"},
                    "to_song": {"type": "string", "description": "Ending song (optional)"}
                },
                "required": ["from_song"]
            }
        ),
        Tool(
            name="get_top_estimated_prophet",
            description="Get the top 10 longest Estimated Prophet performances ranked by duration, with dates and venues.",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": []
            }
        ),
        Tool(
            name="search_deadcasts",
            description="Search Deadcast podcast transcripts for a term or topic.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search term (e.g., 'Jerry Garcia', '1972', 'Venom')"}
                },
                "required": ["query"]
            }
        ),
        Tool(
            name="ask_gd_bot",
            description="Ask a natural language question about the Grateful Dead. Uses LLM to synthesize an answer from the archive data with citations to specific shows.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Natural language question (e.g., 'What are the best 1977 Dark Stars?')"},
                    "use_openai": {"type": "boolean", "description": "Use OpenAI for better answers (requires OPENAI_API_KEY)", "default": False}
                },
                "required": ["query"]
            }
        )
    ])


async def handle_call_tool(name: str, arguments: dict[str, Any]):
    """Handle tool calls."""
    load_resources()

    if name == "search_comments":
        return await _tool_search_comments(arguments)
    elif name == "search_songs":
        return await _tool_search_songs(arguments)
    elif name == "search_guests":
        return await _tool_search_guests(arguments)
    elif name == "search_transitions":
        return await _tool_search_transitions(arguments)
    elif name == "get_top_estimated_prophet":
        return await _tool_get_top_ep(arguments)
    elif name == "search_deadcasts":
        return await _tool_search_deadcasts(arguments)
    elif name == "ask_gd_bot":
        return await _tool_ask_gd_bot(arguments)

    return _build_text_response(f"Unknown tool: {name}")


async def _tool_search_comments(arguments: dict) -> CallToolResult:
    query = arguments.get("query", "")
    k = int(arguments.get("k", 10))

    if not _cache.get('index') or not _cache.get('model'):
        return _build_text_response("Index not available. The FAISS index needs to be rebuilt.")

    q_vec = _cache['model'].encode([query], convert_to_numpy=True)
    distances, indices = _cache['index'].search(q_vec, k)

    results = []
    metadata = _cache.get('metadata', [])
    for dist, idx in zip(distances[0], indices[0]):
        if idx < 0 or idx >= len(metadata):
            continue
        m = metadata[idx]
        results.append({
            "show_id": m.get('show_identifier', ''),
            "url": f"https://archive.org/details/{m.get('show_identifier', '')}",
            "score": float(dist),
            "rating": m.get('rating', ''),
            "date": m.get('created', ''),
            "reviewer": m.get('reviewer', 'anonymous'),
            "text": m.get('comment_text', '')[:500],
            "type": m.get('type', 'comment')
        })

    response_parts = [f"## Search results for: {query}"]
    for i, r in enumerate(results, 1):
        response_parts.append(
            f"**#{i}** [{r['show_id']}]({r['url']}) — Score: {r['score']:.3f}\n"
            f"Rating: {r['rating']} | Date: {r['date']}\n"
            f"_{r['text']}..._\n"
        )
    response_parts.append(f"\n*{len(results)} results found.*")

    return _build_text_response("\n".join(response_parts))


async def _tool_search_songs(arguments: dict) -> CallToolResult:
    song_name = arguments.get("song_name", "").lower()
    metadata = _cache.get('metadata', [])

    matches = [m for m in metadata
               if m.get("type") == "setlist_song"
               and song_name in m.get("song_name", "").lower()]

    response_parts = [f"## Performances of '{song_name.title()}'"]
    for i, m in enumerate(matches[:15], 1):
        show_id = m.get('show_identifier', '')
        dur = m.get('duration', '')
        dur_str = f" | Duration: {dur}" if dur else ""
        response_parts.append(
            f"**#{i}** [{show_id}](https://archive.org/details/{show_id})\n"
            f"Date: {m.get('created', 'N/A')}{dur_str}\n"
            f"_{m.get('comment_text', '')[:200]}..._\n"
        )
    response_parts.append(f"\n*{len(matches)} performances found.*")

    return _build_text_response("\n".join(response_parts))


async def _tool_search_guests(arguments: dict) -> CallToolResult:
    guest_name = arguments.get("guest_name", "").lower()
    metadata = _cache.get('metadata', [])

    matches = [m for m in metadata
               if m.get("type") == "guest_artist"
               and guest_name in m.get("guest_artist", "").lower()]

    response_parts = [f"## Guest appearances by '{guest_name.title()}'"]
    for i, m in enumerate(matches[:15], 1):
        show_id = m.get('show_identifier', '')
        response_parts.append(
            f"**#{i}** [{show_id}](https://archive.org/details/{show_id})\n"
            f"Date: {m.get('created', 'N/A')}\n"
        )
    response_parts.append(f"\n*{len(matches)} appearances found.*")

    return _build_text_response("\n".join(response_parts))


async def _tool_search_transitions(arguments: dict) -> CallToolResult:
    from_song = arguments.get("from_song", "").lower()
    to_song = arguments.get("to_song", "").lower() if arguments.get("to_song") else None
    metadata = _cache.get('metadata', [])

    matches = [m for m in metadata if m.get("type") == "transition"
               and from_song in m.get("transition_from", "").lower()
               and (to_song is None or to_song in m.get("transition_to", "").lower())]

    if to_song:
        response_parts = [f"## Transitions from '{from_song.title()}' to '{to_song.title()}'"]
    else:
        response_parts = [f"## Transitions from '{from_song.title()}'"]

    for i, m in enumerate(matches[:15], 1):
        show_id = m.get('show_identifier', '')
        to_name = m.get('transition_to', 'N/A')
        response_parts.append(
            f"**#{i}** [{show_id}](https://archive.org/details/{show_id})\n"
            f"→ {to_name} | Date: {m.get('created', 'N/A')}\n"
        )
    response_parts.append(f"\n*{len(matches)} transitions found.*")

    return _build_text_response("\n".join(response_parts))


async def _tool_get_top_ep(arguments: dict) -> CallToolResult:
    ep_durations = _cache.get('ep_durations', [])
    if not ep_durations:
        ep_path = PROJECT_DIR / "data" / "ep_durations.json"
        if ep_path.exists():
            with open(ep_path) as f:
                ep_durations = json.load(f)

    if not ep_durations:
        return _build_text_response("Estimated Prophet duration data not yet available.")

    sorted_ep = sorted(
        [d for d in ep_durations if d.get("duration_seconds") is not None],
        key=lambda x: x["duration_seconds"],
        reverse=True
    )[:10]

    response_parts = ["## Top 10 Longest Estimated Prophet Performances"]
    response_parts.append(f"Based on {len(ep_durations)} performances across Grateful Dead shows.\n")

    for i, r in enumerate(sorted_ep, 1):
        dur = r.get('duration_seconds', 0)
        mins = int(dur // 60)
        secs = int(dur % 60)
        dur_str = f"{mins}:{secs:02d}"

        show_id = r.get('show_id', '')
        # Extract date from show_id
        date_match = re.search(r'gd(\d{4}-\d{2}-\d{2})', show_id)
        if date_match:
            date_str = date_match.group(1)
            date_formatted = _format_date(date_str)
            venue = _venue_for_date(date_str, r)
        else:
            date_formatted = "unknown date"
            venue = "Unknown venue"

        response_parts.append(
            f"**#{i}** | {date_formatted} | {venue} | **{dur_str}**\n"
            f"[{show_id}](https://archive.org/details/{show_id})\n"
        )

    response_parts.append("\n**Note**: #1 (Sep 2, 1978 at 32:22) includes the transition into Scarlet Begonias.")
    response_parts.append(f"\n*Total: {len(ep_durations)} performances found.*")

    return _build_text_response("\n".join(response_parts))


async def _tool_search_deadcasts(arguments: dict) -> CallToolResult:
    query = arguments.get("query", "").lower()
    deadcast = _cache.get('deadcast_transcripts', [])
    if not deadcast:
        dc_path = PROJECT_DIR / "data" / "deadcast_transcripts"
        if dc_path.exists():
            with open(dc_path) as f:
                deadcast = json.load(f)

    matches = []
    for dt in deadcast:
        title = dt.get("title", "")
        transcript = dt.get("transcript", "")
        if query in title.lower() or query in transcript.lower():
            matches.append(dt)

    response_parts = [f"## Deadcast results for: {query}"]
    for i, dt in enumerate(matches[:10], 1):
        title = dt.get("title", "")
        url = dt.get("url", "")
        wc = dt.get("word_count", 0)
        # Find context around match
        transcript = dt.get("transcript", "")
        context = ""
        for field in [title, transcript]:
            idx = field.lower().find(query)
            if idx >= 0:
                start = max(0, idx - 100)
                end = min(len(field), idx + 300)
                context = field[start:end]
                break

        response_parts.append(
            f"**#{i}** [{title}]({url}) ({wc} words)\n"
            f"_{context[:300]}..._\n"
        )
    response_parts.append(f"\n*{len(matches)} episodes found.*")

    return _build_text_response("\n".join(response_parts))


async def _tool_ask_gd_bot(arguments: dict) -> CallToolResult:
    query = arguments.get("query", "")
    use_openai = arguments.get("use_openai", False)

    metadata = _cache.get('metadata', [])
    if _cache.get('index') and _cache.get('model'):
        q_vec = _cache['model'].encode([query], convert_to_numpy=True)
        distances, indices = _cache['index'].search(q_vec, 15)
        results = []
        for dist, idx in zip(distances[0], indices[0]):
            if idx < 0 or idx >= len(metadata):
                continue
            results.append({"meta": metadata[idx]})
    else:
        results = []

    if use_openai or os.environ.get("OPENAI_API_KEY"):
        api_key = os.environ.get("OPENAI_API_KEY", "")
        if api_key:
            try:
                from openai import OpenAI
                client = OpenAI(api_key=api_key)

                context_parts = []
                for i, r in enumerate(results, 1):
                    m = r.get("meta", {})
                    text = m.get("comment_text", "")[:300]
                    show = m.get("show_identifier", "")
                    context_parts.append(
                        f"[{i}] Show: {show} (rating: {m.get('rating','N/A')}, date: {m.get('created','N/A')})\nComment: {text}"
                    )

                prompt = f"""You are "Hal," a Grateful Dead knowledge bot. The user asked: "{query}"

Below are {len(results)} relevant results from the GD archive.
Synthesize a helpful, well-structured answer that:
1. Summarizes the key points
2. Cites specific shows with their archive.org links
3. Includes quoted excerpts where useful
4. Stays grounded in the provided data — do NOT invent facts

Source material:
{chr(10).join(context_parts)}
"""
                resp = client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.3,
                    max_tokens=1000
                )
                return _build_text_response(resp.choices[0].message.content.strip())
            except Exception as e:
                pass

    # Fallback to extractive summary
    response_parts = [f"**Answer to: \"{query}\"**"]
    response_parts.append(f"Based on {len(results)} sources from the GD archive:\n")

    for i, r in enumerate(results, 1):
        m = r.get("meta", {})
        text = m.get("comment_text", "")
        if len(text) > 300:
            text = text[:300] + "..."
        response_parts.append(
            f"**#{i}** — [{m.get('show_identifier', 'N/A')}](https://archive.org/details/{m.get('show_identifier', '')})\n"
            f"Rating: {m.get('rating','N/A')}/5 | Date: {m.get('created', 'N/A')}\n"
            f"_{text}_\n"
        )
    response_parts.append("\n---\n*No OpenAI API key set. For synthesized answers, configure `OPENAI_API_KEY`.*")

    return _build_text_response("\n".join(response_parts))


# Create the server instance with callbacks
server = Server(
    "lemieux",
    on_list_tools=handle_list_tools,
    on_call_tool=handle_call_tool,
)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--query":
        # Standalone mode
        query = " ".join(sys.argv[2:])
        load_resources()

        if _cache.get('index') and _cache.get('model'):
            q_vec = _cache['model'].encode([query], convert_to_numpy=True)
            distances, indices = _cache['index'].search(q_vec, 10)

            results = []
            metadata = _cache.get('metadata', [])
            for dist, idx in zip(distances[0], indices[0]):
                if idx < 0 or idx >= len(metadata):
                    continue
                m = metadata[idx]
                results.append((dist, m))

            print(f"\n=== Results for: {query} ===\n")
            for i, (dist, m) in enumerate(results, 1):
                print(f"#{i} [{m.get('show_identifier', '')}] (score: {dist:.3f})")
                print(f"  {m.get('comment_text', '')[:300]}...")
                print(f"  {m.get('created', '')} | {m.get('rating', '')}/5\n")
        else:
            print("Index not available.")
    elif len(sys.argv) > 1 and sys.argv[1] == "--test-ep":
        # Test EP durations
        load_resources()
        ep_durations = _cache.get('ep_durations', [])
        if not ep_durations:
            print("No EP durations found.")
        else:
            sorted_ep = sorted(
                [d for d in ep_durations if d.get("duration_seconds") is not None],
                key=lambda x: x["duration_seconds"],
                reverse=True
            )[:10]
            print(f"\n=== Top 10 Longest Estimated Prophet ({len(ep_durations)} total) ===\n")
            for i, r in enumerate(sorted_ep, 1):
                dur = r.get('duration_seconds', 0)
                mins = int(dur // 60)
                secs = int(dur % 60)
                show_id = r.get('show_id', '')
                date_match = re.search(r'gd(\d{4}-\d{2}-\d{2})', show_id)
                if date_match:
                    date_str = date_match.group(1)
                    date_fmt = _format_date(date_str)
                    venue = _venue_for_date(date_str, r)
                else:
                    date_fmt = "unknown"
                    venue = "Unknown venue"
                print(f"#{i} | {date_fmt} | {venue} | {mins}:{secs:02d}")
            print()
    else:
        # Default: MCP stdio server mode
        async def stdio_run():
            async with stdio.stdio_server() as (read_stream, write_stream):
                await server.run(
                    read_stream,
                    write_stream,
                    InitializationOptions(server=server.server_info)
                )
        asyncio.run(stdio_run())
