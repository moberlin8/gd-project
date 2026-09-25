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
from datetime import datetime
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
LLM_MODEL = os.environ.get("LEMIEUX_LLM_MODEL", "poolside/laguna-s-2.1:free")
LLM_MODEL_FALLBACK = os.environ.get("LEMIEUX_LLM_MODEL_FALLBACK", "qwen/qwen3.8-omni-flash")
LLM_BASE_URL = os.environ.get("LEMIEUX_LLM_BASE_URL", "https://inference-api.nousresearch.com/v1")

# Path to the Hermes-shared auth file whose access_token the runtime refreshes
# ~hourly. Used as a live fallback key when XAI_API_KEY is missing/stale.
NOUS_AUTH_PATH = Path("/home/hermes/.hermes/shared/nous_auth.json")

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
    """Vector search over the whole index.

    When ``query`` carries a recognizable date (e.g. 'September 25',
    '9/25', '1974-09-25', 'today'), the results are augmented with
    metadata entries whose show date matches — the vector text for
    setlist entries doesn't include the date, so date questions would
    otherwise miss them entirely.
    """
    q_vec = model.encode([query], convert_to_numpy=True)
    distances, indices = index.search(q_vec, k)
    results = []
    for dist, idx in zip(distances[0], indices[0]):
        if idx < 0 or idx >= len(metadata):
            continue
        results.append({"score": float(dist), "meta": metadata[idx]})
    _augment_with_date_results(metadata, query, results)
    return results


# ── Date-aware search ─────────────────────────────────────────────────────────
_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6,
    "july": 7, "jul": 7, "august": 8, "aug": 8, "september": 9, "sep": 9,
    "sept": 9, "october": 10, "oct": 10, "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}

# Word-form numbers → integer day (1–31). Covers the full range a calendar
# day can take, including compound forms like "twenty-fifth" / "twenty five".
_WORD_NUMS = {
    "one": 1, "first": 1, "two": 2, "second": 2, "three": 3, "third": 3,
    "four": 4, "fourth": 4, "five": 5, "fifth": 5, "six": 6, "sixth": 6,
    "seven": 7, "seventh": 7, "eight": 8, "eighth": 8, "nine": 9, "ninth": 9,
    "ten": 10, "tenth": 10, "eleven": 11, "eleventh": 11, "twelve": 12,
    "twelfth": 12, "thirteen": 13, "thirteenth": 13, "fourteen": 14,
    "fourteenth": 14, "fifteen": 15, "fifteenth": 15, "sixteen": 16,
    "sixteenth": 16, "seventeen": 17, "seventeenth": 17, "eighteen": 18,
    "eighteenth": 18, "nineteen": 19, "nineteenth": 19, "twenty": 20,
    "twentieth": 20, "thirty": 30, "thirtieth": 30, "thirty-one": 31,
    "thirty-first": 31,
}


def _word_to_int(s: str) -> Optional[int]:
    """Convert a word-form number (e.g. 'twenty-fifth', 'twenty five') to int.

    Handles 1–31 including compound tens+ones. Returns None if not a number.
    """
    s = s.strip().lower().replace("-", " ")
    parts = [p for p in s.split() if p]
    if not parts:
        return None
    # Single word
    if len(parts) == 1:
        return _WORD_NUMS.get(parts[0])
    # Compound "twenty five" / "twenty fifth" → tens + ones
    tens = _WORD_NUMS.get(parts[0])
    if tens is None or tens < 20:
        return None
    ones = _WORD_NUMS.get(parts[1])
    if ones is None or ones > 9:
        return None
    return tens + ones


def _parse_date_query(query) -> Optional[dict]:
    """Extract date information from a user query.

    Returns a dict with ``month``/``day`` (and ``year`` when present), or
    ``None`` if no usable date is found. Recognizes:

      - 'September 25' / 'Sep 25' / 'September 25, 1974'  → month, day [, year]
      - 'September twenty-fifth' / 'Sep twenty five'      → word-form day
      - '9/25' or '09/25'                                  → month=9, day=25
      - '9-25' or '09-25'                                  → month=9, day=25 (dash date)
      - '1974-09-25' or 'gd1974-09-25'                     → year, month, day
      - 'today'                                            → current month/day/year
    """
    if not query or not isinstance(query, str):
        return None
    low = query.lower()
    now = datetime.now()

    # 'today' → current date
    if re.search(r"\btoday\b", low):
        return {"month": now.month, "day": now.day, "year": now.year}

    # 'YYYY-MM-DD' (optionally inside a gdYYYY-MM-DD show identifier)
    m = re.search(r"(?:gd)?(\d{4})-(\d{1,2})-(\d{1,2})", query)
    if m:
        return {
            "year": int(m.group(1)),
            "month": int(m.group(2)),
            "day": int(m.group(3)),
        }

    # Numeric 'M/D'
    m = re.search(r"(?<![\d/])(\d{1,2})/(\d{1,2})(?![\d/])", query)
    if m:
        mo, d = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return {"month": mo, "day": d}

    # Ambiguous dash date 'M-D' (e.g. '9-25' / '09-25'). Same month/day
    # convention as 'M/D'. The full 'YYYY-MM-DD' form is handled above, so
    # this only fires for short dash dates.
    m = re.search(r"(?<![\d-])(\d{1,2})-(\d{1,2})(?![\d-])", query)
    if m:
        mo, d = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return {"month": mo, "day": d}

    # Month name + day (+ optional year), numeric day
    m = re.search(
        r"\b([A-Za-z]{3,9})\s+(\d{1,2})(?:st|nd|rd|th)?(?:\s*,?\s*(\d{4}))?\b", low
    )
    if m:
        month = _MONTHS.get(m.group(1))
        if month is None:
            prefix = m.group(1)[:3]
            for name, num in _MONTHS.items():
                if name.startswith(prefix):
                    month = num
                    break
        day = int(m.group(2))
        if month and 1 <= day <= 31:
            info = {"month": month, "day": day}
            if m.group(3):
                info["year"] = int(m.group(3))
            return info

    # Month name + word-form day (e.g. 'September twenty-fifth',
    # 'Sep twenty five'). Optional year follows the day.
    # Use an explicit alternation of known month names so that preceding
    # words like "shows" or "about" don't get greedy-matched as the month.
    _mon_alt = "|".join(sorted(_MONTHS, key=len, reverse=True))
    m = re.search(
        rf"\b({_mon_alt})\s+([a-z]+(?:[\s-][a-z]+)?)(?:\s*,?\s*(\d{{4}}))?\b",
        low,
    )
    if m:
        month = _MONTHS.get(m.group(1))
        if month is None:
            prefix = m.group(1)[:3]
            for name, num in _MONTHS.items():
                if name.startswith(prefix):
                    month = num
                    break
        day = _word_to_int(m.group(2))
        if month and day and 1 <= day <= 31:
            info = {"month": month, "day": day}
            if m.group(3):
                info["year"] = int(m.group(3))
            return info
    return None


def _resolve_today(query: str) -> str:
    """Replace standalone 'today' in a query with the current date text.

    e.g. 'What shows today' → 'What shows September 25, 2026'
    """
    if not query or not re.search(r"\btoday\b", query, flags=re.IGNORECASE):
        return query
    return re.sub(
        r"\btoday\b", datetime.now().strftime("%B %d, %Y"), query, flags=re.IGNORECASE
    )


def _metadata_show_date(m) -> Optional[tuple]:
    """Return (year, month, day) of the show a metadata entry belongs to.

    Reads the date from ``show_identifier`` (gdYYYY-MM-DD...) and falls back
    to the 'created' field for ``setlist_song`` entries, which store the show
    date there (YYYY-MM-DD). Returns None when no date can be determined.
    """
    sid = m.get("show_identifier") or ""
    g = re.search(r"(?:gd)?(\d{2,4})-(\d{2})-(\d{2})", sid)
    if g:
        y = int(g.group(1))
        if y < 100:
            y = 1900 + y if y > 30 else 2000 + y
        return y, int(g.group(2)), int(g.group(3))
    if m.get("type") == "setlist_song":
        created = m.get("created") or ""
        g2 = re.search(r"^(\d{4})-(\d{2})-(\d{2})", created)
        if g2:
            return int(g2.group(1)), int(g2.group(2)), int(g2.group(3))
    return None


def _date_matches(mo: int, d: int, y: int, month: int, day: int, year) -> bool:
    """Match a show date against a parsed date query.

    With a year → exact match. Without a year → cross-year month/day match
    (all shows ever played on that calendar date).
    """
    if year is not None:
        return y == year and mo == month and d == day
    return mo == month and d == day


def _filter_metadata_by_date(metadata, date_info) -> list:
    """Return metadata entries whose show date matches ``date_info``.

    ``date_info`` is the dict from ``_parse_date_query``. Matching is by
    show date (month+day across years, or full year+month+day when a year
    is given). Order of the input list is preserved.
    """
    if not date_info or not isinstance(metadata, list):
        return []
    month = date_info.get("month")
    day = date_info.get("day")
    year = date_info.get("year")
    if not month or not day:
        return []
    out = []
    for m in metadata:
        d = _metadata_show_date(m)
        if d and _date_matches(d[1], d[2], d[0], month, day, year):
            out.append(m)
    return out


def _augment_with_date_results(metadata, query, results) -> None:
    """Merge date-filtered metadata entries into ``results`` (in place).

    Called when a query contains a date, so shows on that date surface even
    though the setlist vector text omits the date. Deduplicated by
    show_identifier+type (via full dict equality on the meta).
    """
    date_info = _parse_date_query(query)
    if not date_info:
        return
    for m in _filter_metadata_by_date(metadata, date_info):
        if not any(r["meta"] == m for r in results):
            results.append({"score": 1.0, "meta": m})


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


def _get_nous_token() -> Optional[str]:
    """Return the live Nous access_token from the Hermes shared auth file.

    The runtime refreshes this file ~hourly, so it stays valid even after the
    token baked into .env (XAI_API_KEY) has expired. Returns None if the file
    is missing or carries no access_token.
    """
    try:
        with open(NOUS_AUTH_PATH, encoding="utf-8") as f:
            data = json.load(f)
        tok = data.get("access_token")
        return tok if isinstance(tok, str) and tok else None
    except (OSError, ValueError):
        return None


def summarize_with_llm(query: str, results: list[dict],
                       api_key: Optional[str] = None,
                       metadata: Optional[list] = None) -> Optional[str]:
    """Synthesize an answer from retrieved hits. Returns None on any failure.

    * The current date is always injected into the system prompt so the model
      knows what 'today' means.
    * A standalone 'today' in ``query`` is expanded to the literal calendar
      date so vector retrieval treats it as a real date.
    * When ``metadata`` (the full index metadata) is supplied and the query
      contains a date, shows on that date are filtered by metadata and merged
      into ``results`` — covering date queries even when the caller didn't
      run them through :func:`search`.

    Key resolution: explicit ``api_key`` > ``XAI_API_KEY`` env > Nous
    ``nous_auth.json``. If the primary key is absent, uses the dynamized token
    directly; if the primary key 401s, retries once with the dynamized token
    (which the Hermes runtime refreshes ~hourly) so a stale .env token no
    longer silently degrades to extractive-only answers.

    Model fallback: if the primary model (``LEMIEUX_LLM_MODEL``) returns no
    usable output, retries with ``LEMIEUX_LLM_MODEL_FALLBACK`` (default:
    poolside/laguna-s-2.1:free) so a flaky free tier never silently drops
    analysis.
    """
    today = datetime.now().strftime("%B %d, %Y")
    system_prompt = SYSTEM_PROMPT + f"\n\nToday is {today}."
    query = _resolve_today(query)

    # Date-aware merge: surface shows on the queried date.
    if isinstance(metadata, list):
        _augment_with_date_results(metadata, query, results)

    auth_token = _get_nous_token()
    models = [LLM_MODEL]
    if LLM_MODEL_FALLBACK and LLM_MODEL_FALLBACK not in models:
        models.append(LLM_MODEL_FALLBACK)

    for model in models:
        primary = api_key or os.environ.get(LLM_KEY_ENV)
        keys = [primary] if primary else []
        if auth_token and auth_token not in keys:
            keys.append(auth_token)
        if not keys:
            continue

        for idx, key in enumerate(keys):
            try:
                from openai import OpenAI
                client = OpenAI(api_key=key, base_url=LLM_BASE_URL, timeout=30)
                user_msg = (
                    f'Question: "{query}"\n\n'
                    f"Retrieved sources ({len(results)}):\n\n{build_context(results)}"
                )
                resp = client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_msg},
                    ],
                    temperature=0.3,
                    max_tokens=900,
                )
                content = resp.choices[0].message.content
                if content and content.strip():
                    return content.strip()
                # Empty content — try next key, then fallback model
            except Exception as e:
                status = getattr(e, "status_code", None)
                # On 401 keep going to the next key;
                # any other failure ends this model attempt.
                if not (status == 401 and idx < len(keys) - 1):
                    break

        if model != models[-1]:
            print(f"[INFO] {model} produced no usable output; "
                  f"retrying with {models[-1]}", file=sys.stderr)

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
