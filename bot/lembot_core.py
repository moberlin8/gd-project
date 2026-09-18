#!/usr/bin/env python3
"""
Lemieux GD RAT — shared core
============================
Retrieval + formatting + LLM synthesis used by both front-ends:
    lembot_telegram.py  (Telegram)
    lembot_discord.py   (Discord, text-only)

Index: FAISS + all-MiniLM-L6-v2 over archive.org comments, setlists,
lyrics/interpretations and book chunks (see index/index_metadata.json).

LLM: any OpenAI-compatible endpoint. Defaults to xAI Grok:
    XAI_API_KEY            required for synthesized 'ask' answers
    LEMIEUX_LLM_MODEL      default grok-4.3
    LEMIEUX_LLM_BASE_URL   default https://api.x.ai/v1
Falls back to extractive results if no key / the call fails.
"""

import json
import os
import re
import sys
from pathlib import Path
from typing import Optional

import faiss
from sentence_transformers import SentenceTransformer

# ── Paths ─────────────────────────────────────────────────────────────────────
PROJECT_DIR = Path("/home/mao/DaveMatt/gd-project")
INDEX_DIR = PROJECT_DIR / "index"
INDEX_PATH = INDEX_DIR / "vector_index.faiss"
META_PATH = INDEX_DIR / "index_metadata.json"

# ── LLM config ────────────────────────────────────────────────────────────────
LLM_KEY_ENV = "XAI_API_KEY"
LLM_MODEL = os.environ.get("LEMIEUX_LLM_MODEL", "grok-4.3")
LLM_BASE_URL = os.environ.get("LEMIEUX_LLM_BASE_URL", "https://api.x.ai/v1")

BOT_NAME = "Lemieux"


# ── Loading ───────────────────────────────────────────────────────────────────
def load_rat():
    """Load FAISS index, metadata, and embedding model.

    Returns (index, metadata, lyrics, model). ``metadata`` is the flat list
    aligned to the FAISS index; ``lyrics`` is the dedicated lyric/interpretation
    list from the metadata file (empty list for legacy flat-list files).
    """
    if not INDEX_PATH.exists():
        raise FileNotFoundError(f"FAISS index not found at {INDEX_PATH}")
    index = faiss.read_index(str(INDEX_PATH))
    with open(META_PATH, encoding="utf-8") as f:
        raw = json.load(f)
    if isinstance(raw, list):
        metadata = raw
        lyrics = raw
    else:
        metadata = raw.get("comments", [])
        lyrics = raw.get("lyrics", [])
    model = SentenceTransformer("all-MiniLM-L6-v2")
    return index, metadata, lyrics, model


# ── Search ────────────────────────────────────────────────────────────────────
def search(index, metadata, model, query: str, k: int = 10):
    """Vector search over the whole index."""
    q_vec = model.encode([query], convert_to_numpy=True)
    distances, indices = index.search(q_vec, k)
    results = []
    for dist, idx in zip(distances[0], indices[0]):
        if idx < 0 or idx >= len(metadata):
            continue
        results.append({"score": float(dist), "meta": metadata[idx]})
    return results


def _norm_name(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()


def search_lyrics(index, metadata, model, query: str, k: int = 15,
                  target_types=("lyric", "interpretation")):
    """Find lyric + interpretation entries for a song.

    Song-name queries drown in setlist vectors, so rank lyric/interpretation
    entries by song_name match first, then top up with filtered vector hits.
    """
    q = _norm_name(query)
    q_tokens = set(q.split())

    scored = []
    for m in metadata:
        if m.get("type") not in target_types or not m.get("song_name"):
            continue
        nm = _norm_name(m.get("song_name", ""))
        if not nm:
            continue
        if nm == q:
            score = 1.0
        elif q and (q in nm or nm in q):
            score = 0.8
        else:
            inter = len(q_tokens & set(nm.split()))
            denom = max(len(q_tokens), len(nm.split()), 1)
            score = 0.5 * inter / denom
        scored.append((score, m))

    scored.sort(key=lambda x: x[0], reverse=True)
    hits = [m for s, m in scored if s > 0.0]

    if len(hits) < k:
        q_vec = model.encode([query], convert_to_numpy=True)
        distances, indices = index.search(q_vec, max(k * 4, 20))
        for dist, idx in zip(distances[0], indices[0]):
            if idx < 0 or idx >= len(metadata):
                continue
            m = metadata[idx]
            if m.get("type") in target_types and m not in hits:
                hits.append(m)
            if len(hits) >= k:
                break

    return [{"score": 1.0 - i * 0.001, "meta": m} for i, m in enumerate(hits[:k])]


def show_url(show_id: str) -> str:
    return f"https://archive.org/details/{show_id}"


# ── Formatting (plain text; safe on both platforms) ───────────────────────────
def format_lyrics(query: str, results: list[dict], max_lyrics: int = 1) -> str:
    lyric_hits = [r for r in results if r["meta"].get("type") == "lyric"]
    if not lyric_hits:
        return f'No lyrics found matching "{query}". Try meaning "song name".'
    lines = [f"LYRICS — {query}\n"]
    for r in lyric_hits[:max_lyrics]:
        m = r["meta"]
        urls = m.get("source_urls", []) or []
        url_block = "\n".join(f"  • {u}" for u in urls) if urls else "  (no source URLs)"
        lyricists = ", ".join(m.get("lyricists", []) or []) or "n/a"
        music_by = ", ".join(m.get("music_by", []) or []) or "n/a"
        lines.append(
            f"Song: {m.get('song_name', m.get('title', '?'))}\n"
            f"Lyricists: {lyricists} | Music: {music_by}\n"
            f"Sources:\n{url_block}\n\n"
            f"{m.get('comment_text', '')}"
        )
    lines.append('\n\nTip: use meaning "song name" for lyrics + interpretations.')
    return "\n".join(lines)


def format_meaning(query: str, results: list[dict]) -> str:
    lyric_hits = [r for r in results if r["meta"].get("type") == "lyric"]
    interp_hits = [r for r in results if r["meta"].get("type") == "interpretation"]
    if not lyric_hits and not interp_hits:
        return f'No lyrics or interpretations found matching "{query}".'
    lines = [f"MEANING — {query}\n"]
    if lyric_hits:
        m = lyric_hits[0]["meta"]
        urls = m.get("source_urls", []) or []
        url_block = "\n".join(f"  • {u}" for u in urls) if urls else "  (no source URLs)"
        lines.append(
            f"Song: {m.get('song_name', m.get('title', '?'))}\n"
            f"Sources:\n{url_block}\n\n"
            f"{m.get('comment_text', '')}"
        )
    if interp_hits:
        lines.append("\n\nINTERPRETATIONS")
        for i, r in enumerate(interp_hits, 1):
            m = r["meta"]
            src = m.get("interpretation_source", "")
            iurl = m.get("interpretation_url", "")
            header = f"{i}. {src}" + (f" — {iurl}" if iurl else "")
            lines.append(f"\n{header}\n{m.get('comment_text', '')[:800]}")
    return "\n".join(lines)


def _source_label(m: dict) -> str:
    """Human label for a hit: show id, book title, or song."""
    t = m.get("type", "comment")
    if t == "book_chunk":
        return f"book: {m.get('book_title') or m.get('title') or '?'}"
    if m.get("show_identifier"):
        return m["show_identifier"]
    return m.get("song_name") or t


# ── LLM synthesis ─────────────────────────────────────────────────────────────
def build_context(results: list[dict], max_chars: int = 600) -> str:
    parts = []
    for i, r in enumerate(results, 1):
        m = r["meta"]
        head = f"[{i}] {_source_label(m)}"
        if m.get("show_identifier"):
            head += f" — {show_url(m['show_identifier'])}"
        if m.get("rating"):
            head += f" (rating {m['rating']}/5)"
        if m.get("created"):
            head += f" posted {m['created']}"
        parts.append(f"{head}\n{m.get('comment_text', '')[:max_chars]}")
    return "\n\n".join(parts)


SYSTEM_PROMPT = (
    f"You are {BOT_NAME}, a Grateful Dead archive bot answering in a Discord/Telegram "
    "chat. You answer ONLY from the retrieved source passages you are given: "
    "archive.org fan reviews, setlist entries, lyrics, interpretations, and book "
    "excerpts. Be concise (under 1500 characters), cite shows by date and include "
    "their archive.org link when you use them, quote short excerpts when they help. "
    "If the sources do not answer the question, say so plainly rather than guessing."
)


def summarize_with_llm(query: str, results: list[dict],
                       api_key: Optional[str] = None) -> Optional[str]:
    """Synthesize an answer from retrieved hits. Returns None on any failure."""
    api_key = api_key or os.environ.get(LLM_KEY_ENV)
    if not api_key:
        return None
    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key, base_url=LLM_BASE_URL)
        user_msg = (
            f'Question: "{query}"\n\n'
            f"Retrieved sources ({len(results)}):\n\n{build_context(results)}"
        )
        resp = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.3,
            max_tokens=900,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        print(f"[WARN] LLM synthesis failed ({LLM_MODEL} @ {LLM_BASE_URL}): {e}",
              file=sys.stderr)
        return None


# ── Smoke test ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("Loading index...")
    idx, meta, lyr, mdl = load_rat()
    print(f"entries={len(meta)} lyrics={len(lyr)} ntotal={idx.ntotal}")
    q = " ".join(sys.argv[1:]) or "best Dark Star of 1972"
    hits = search(idx, meta, mdl, q, k=8)
    for h in hits[:3]:
        print(f"- {_source_label(h['meta'])}: {h['meta'].get('comment_text','')[:100]!r}")
    ans = summarize_with_llm(q, hits)
    print("\nLLM answer:" if ans else "\nLLM unavailable — extractive only")
    if ans:
        print(ans)
