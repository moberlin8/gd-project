#!/usr/bin/env python3
"""
Lemieux GD RAT Telegram Bot
============================
Grateful Dead RAG bot on Telegram, powered by the same FAISS vector index
used by the Hermes MCP server (lemieux).

Architecture:
    [Telegram User] → [@BotFather token] → python-telegram-bot →
    FAISS + SentenceTransformer → (optional OpenAI LLM summary) →
    back to Telegram chat

Voice note support:
    Placeholder for future integration with cloned "Lemieux" voice.
    When `ENABLE_LEMIEU_VOICE=true` and piper model is present,
    /gd ask "query" --voice will synthesize the LLM summary via Piper
    and return an audio file. Currently disabled — text-only.

Setup:
    1. Talk to @BotFather on Telegram, run /newbot
    2. Set LEMIEUX_TELEGRAM_TOKEN=<token>
    3. Optional: set OPENAI_API_KEY for LLM-synthesized answers
    4. python3 lembot_telegram.py

Commands:
    /gd ask "question"         → Retrieve + summarize GD archive answer
    /gd search "text"           → Show raw matching comments
    /start                      → Welcome message
    /help                       → Show usage

Author: Hal (Douglas Raines persona system)
        Grateful Dead division of Nous Research
"""

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Optional

import faiss
import numpy as np
from sentence_transformers import SentenceTransformer

# python-telegram-bot v20+ async API
from telegram import Update, InputFile
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ── Paths ─────────────────────────────────────────────────────────────────────
PROJECT_DIR = Path("/home/mao/DaveMatt/gd-project")
INDEX_DIR = PROJECT_DIR / "index"
INDEX_PATH = INDEX_DIR / "vector_index.faiss"
META_PATH = INDEX_DIR / "index_metadata.json"

# ── Constants ─────────────────────────────────────────────────────────────────
TOKEN_ENV_KEY = "LEMIEUX_TELEGRAM_TOKEN"
OPENAI_KEY_ENV = "OPENAI_API_KEY"
PIPER_BINARY = shutil.which("piper")  # may be None
PIPER_MODEL_DIR = Path("/home/hermes/.hermes/hal_voices/piper_hal_model")
WAV_OUTPUT_DIR = PROJECT_DIR / "bot" / "output"
WAV_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Placeholder: only activates once Lemur voice is trained
ENABLE_LEMIEU_VOICE = os.environ.get("ENABLE_LEMIEU_VOICE", "false").lower() == "true"


# ── RAG Core (duplicated from Hermes MCP for independence) ─────────────────
def load_rat():
    """Load FAISS index, metadata, and embedding model.

    Returns (index, metadata, lyrics, model) where ``metadata`` is the flat
    list aligned to the FAISS index (contains lyric + interpretation entries
    too) and ``lyrics`` is the dedicated 'lyrics' key (lyric/interpretation
    entries only).
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
        metadata = raw.get('comments', [])
        lyrics = raw.get('lyrics', [])
    model = SentenceTransformer("all-MiniLM-L6-v2")
    return index, metadata, lyrics, model


def search(index, metadata, model, query: str, k: int = 10):
    """Vector search over GD archive comments."""
    q_vec = model.encode([query], convert_to_numpy=True)
    distances, indices = index.search(q_vec, k)
    results = []
    for dist, idx in zip(distances[0], indices[0]):
        if idx < 0 or idx >= len(metadata):
            continue
        results.append({"score": float(dist), "meta": metadata[idx]})
    return results


def _norm_name(s: str) -> str:
    """Lowercase, strip punctuation for fuzzy name matching."""
    return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()


def search_lyrics(index, metadata, model, query: str, k: int = 15,
                  target_types=("lyric", "interpretation")):
    """Find lyrics + interpretation entries for a song lookup.

    Song-name queries are easily crowded out of a pure vector search by the
    thousands of setlist vectors (each song appears in many shows), so we rank
    lyric/interpretation entries by song_name match first and top up the rest
    with filtered vector-search hits. Entries are ordered lyrics before
    interpretations by the caller's format functions.
    """
    q = _norm_name(query)
    q_tokens = set(q.split())

    lyric_entries = [
        m for m in metadata
        if m.get("type") in target_types and m.get("song_name")
    ]

    scored = []
    for m in lyric_entries:
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

    # Top up with filtered vector hits when lexical match comes up short.
    if len(hits) < k:
        q_vec = model.encode([query], convert_to_numpy=True)
        wide = max(k * 4, 20)
        distances, indices = index.search(q_vec, wide)
        for dist, idx in zip(distances[0], indices[0]):
            if idx < 0 or idx >= len(metadata):
                continue
            m = metadata[idx]
            if m.get("type") in target_types and m not in hits:
                hits.append(m)
            if len(hits) >= k:
                break

    return [{"score": 1.0 - i * 0.001, "meta": m} for i, m in enumerate(hits[:k])]


def format_show_url(show_id: str) -> str:
    return f"https://archive.org/details/{show_id}"


def format_results(query: str, results: list[dict], max_items: int = 5) -> str:
    """Format search results as Telegram-friendly markdown."""
    if not results:
        return "No comments found matching that query."
    lines = [f"*Results for:* _{query}_\n"]
    for i, r in enumerate(results[:max_items], 1):
        m = r["meta"]
        text = m.get("comment_text", "")[:400]
        text = text[:-3] + "..." if len(text) > 400 else text
        lines.append(
            f"*{i}* [{m['show_identifier']}]({format_show_url(m['show_identifier'])})\n"
            f"Rating: {m.get('rating', 'N/A')}/5 | Date: {m.get('created', 'N/A')}\n"
            f"{text}\n"
        )
    lines.append(
        f"\n_Top {len(results[:max_items])} of {len(results)} results. "
        f"Use `/gd search \"text\"` for more._"
    )
    return "\n".join(lines)


def format_lyrics(query: str, results: list[dict], max_lyrics: int = 1) -> str:
    """Format a lyric lookup: song title + source URLs + full lyrics text.

    Plain-text output (no parse_mode) so lyric characters can't break
    Telegram Markdown.
    """
    lyric_hits = [r for r in results if r["meta"].get("type") == "lyric"]
    if not lyric_hits:
        return f"No lyrics found matching \"{query}\". Try meaning:\"song name\"."
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
    lines.append("\n\nTip: use meaning:\"song name\" for lyrics + interpretations.")
    return "\n".join(lines)


def format_meaning(query: str, results: list[dict]) -> str:
    """Format a meaning lookup: song lyrics + all interpretations.

    Plain-text output (no parse_mode) to avoid Telegram Markdown breakage.
    """
    lyric_hits = [r for r in results if r["meta"].get("type") == "lyric"]
    interp_hits = [r for r in results if r["meta"].get("type") == "interpretation"]
    if not lyric_hits and not interp_hits:
        return f"No lyrics or interpretations found matching \"{query}\"."
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


def summarize_with_llm(query, results, api_key) -> Optional[str]:
    """Use OpenAI to synthesize a natural-language answer from retrieved comments."""
    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key)
        context_parts = []
        for i, r in enumerate(results, 1):
            m = r["meta"]
            context_parts.append(
                f"[{i}] Show: {m.get('show_identifier', '')} "
                f"(rating: {m.get('rating','N/A')}/5, date: {m.get('created','N/A')})\n"
                f"Comment: {m.get('comment_text','')}"
            )
        context = "\n\n".join(context_parts)
        prompt = (
            f"You are \"Douglas,\" a Grateful Dead knowledge bot. The user asked: \"{query}\"\n\n"
            f"Based on {len(results)} relevant fan comments from archive.org, "
            f"provide a concise answer. Cite specific shows with archive.org links. "
            f"Stay grounded in the comments — do not invent facts.\n\n"
            f"Fan comments:\n{context}"
        )
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=1000,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        print(f"[WARN] OpenAI synthesis failed: {e}", file=sys.stderr)
        return None


# ── Telegram Bot ──────────────────────────────────────────────────────────────
class LemurTGBot:

    def __init__(self, token: str):
        self.token = token
        self.app: Optional[Application] = None
        self.index = None
        self.metadata = None
        self.lyrics = None
        self.model = None
        self.openai_key: Optional[str] = os.environ.get(OPENAI_KEY_ENV)
        self._load_rat()

    def _load_rat(self):
        """Load RAG resources once at startup."""
        print("⏳ Loading FAISS index + embedding model...")
        try:
            self.index, self.metadata, self.lyrics, self.model = load_rat()
            print(f"✅ Loaded: {len(self.metadata)} metadata entries | "
                  f"{len(self.lyrics)} lyrics/interpretations | index size ~{self.index.ntotal}")
        except Exception as e:
            print(f"❌ Failed to load RAG resources: {e}", file=sys.stderr)
            sys.exit(1)

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start."""
        await update.message.reply_text(
            "🎵 *Lemieux GD Bot* — Your Grateful Dead archive assistant.\n\n"
            "Use:\n"
            "/gd ask \"question\"\n"
            "/gd search \"text\"\n"
            "/gd lyrics \"song name\"\n"
            "/gd meaning \"song name\"\n"
            "/help for details."
        )

    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /help."""
        await update.message.reply_text(
            "*Commands:*\n"
            "/start — Show welcome message\n"
            "/help — Show this help\n"
            "/gd ask \"question\" — Synthesize answer from fan reviews\n"
            "/gd search \"text\" — Show raw matching comments\n"
            "/gd lyrics \"song name\" — Show lyrics + source URLs\n"
            "/gd meaning \"song name\" — Show lyrics + all interpretations\n\n"
            "_Powered by the Grateful Dead RAG knowledge base._"
        )

    def _parse_gd_args(self, text: str) -> tuple[str, str]:
        """
        Parse arguments for /gd command.
        Returns (action: str, question: str)
        """
        text = text.strip()
        # Match quoted string
        match = re.match(r'(\w+)\s*"(.+?)"', text)
        if match:
            return match.group(1).lower(), match.group(2)
        # Fallback: split on whitespace
        parts = text.split(None, 1)
        action = parts[0].lower() if parts else "ask"
        question = parts[1] if len(parts) > 1 else ""
        return action, question

    async def gd_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /gd ask/search/lyrics/meaning \"...\"."""
        if not context.args:
            await update.message.reply_text(
                "Usage:\n"
                "/gd ask \"your question\"\n"
                "/gd search \"your search term\"\n"
                "/gd lyrics \"song name\"\n"
                "/gd meaning \"song name\""
            )
            return

        raw_args = " ".join(context.args)
        action, question = self._parse_gd_args(raw_args)

        if action not in ("ask", "search", "lyrics", "meaning"):
            await update.message.reply_text(
                f"Unknown action '{action}'. Use 'ask', 'search', 'lyrics', or 'meaning'."
            )
            return

        await context.bot.send_chat_action(update.effective_chat.id, "typing")

        # Lyrics / meaning lookups (no LLM, plain-text output)
        if action in ("lyrics", "meaning"):
            results = search_lyrics(self.index, self.metadata, self.model, question, k=15)
            if action == "lyrics":
                response = format_lyrics(question, results)
            else:
                response = format_meaning(question, results)
            await update.message.reply_text(
                response, disable_web_page_preview=True
            )
            return

        results = search(self.index, self.metadata, self.model, question, k=15)

        if action == "ask":
            # Try LLM summary; fallback to extractive
            summary = None
            if self.openai_key:
                summary = summarize_with_llm(question, results, self.openai_key)

            if summary:
                await update.message.reply_text(summary, parse_mode="Markdown")
            else:
                extractive = format_results(question, results, max_items=5)
                note = (
                    "\n\n_No OpenAI API key set. "
                    "For synthesized answers, set `OPENAI_API_KEY`._"
                ) if not self.openai_key else ""
                await update.message.reply_text(extractive + note, parse_mode="Markdown")

        elif action == "search":
            response = format_results(question, results, max_items=5)
            await update.message.reply_text(response, parse_mode="Markdown", disable_web_page_preview=True)

    async def voice_ready(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Placeholder handler for future voice flag parsing."""
        await update.message.reply_text(
            "🔊 Voice synthesis for Lemieux is currently offline.\n"
            "Will activate once voice training is complete."
        )

    def run(self):
        """Start the Telegram bot."""
        self.app = Application.builder().token(self.token).build()

        # Register handlers
        self.app.add_handler(CommandHandler("start", self.start))
        self.app.add_handler(CommandHandler("help", self.help))
        self.app.add_handler(CommandHandler("gd", self.gd_command))

        print("🚀 Lemieux GD Telegram Bot starting...")
        print(f"   Voice enabled: {ENABLE_LEMIEU_VOICE}")
        print(f"   OpenAI key: {'✅' if self.openai_key else '❌'}")

        self.app.run_polling()
        print("👋 Bot stopped.")


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Lemieux GD RAT Telegram Bot")
    parser.add_argument(
        "--test",
        action="store_true",
        help="Run a quick smoke test of the RAG pipeline without starting the bot",
    )
    args = parser.parse_args()

    if args.test:
        print("🧪 Smoke testing RAG pipeline...")
        idx, meta, lyr, mdl = load_rat()
        print(f"✅ Index loaded | Entries: {len(meta)} | Lyrics: {len(lyr)} | Model ready")
        # Quick comment search
        results = search(idx, meta, mdl, "Dark Star", k=1)
        print(f"🔍 Sample result: {results[0]['meta'].get('comment_text', 'N/A')[:80]}...")
        # Lyrics smoke test
        lyric_results = search_lyrics(idx, meta, mdl, "China Cat Sunflower", k=5)
        lyric_types = [r["meta"].get("type") for r in lyric_results]
        print(f"🎵 Lyric search types: {lyric_types}")
        if lyric_results:
            m = lyric_results[0]["meta"]
            print(f"🎵 Top lyric: {m.get('song_name', '?')} | {len(m.get('comment_text',''))} chars")
        return

    token = os.environ.get(TOKEN_ENV_KEY)
    if not token:
        print(f"❌ Environment variable {TOKEN_ENV_KEY} not set.", file=sys.stderr)
        print("   Register a bot via @BotFather and set the token.", file=sys.stderr)
        sys.exit(1)

    bot = LemurTGBot(token)
    bot.run()


if __name__ == "__main__":
    main()